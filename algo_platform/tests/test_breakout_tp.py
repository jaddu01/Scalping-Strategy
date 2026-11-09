"""TP1 / TP2 variants: settings, levels, live engine scale-out (with protective SL resizing) and backtest."""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backtest.engine import BacktestConfig, run_backtest
from brokers.base import Candle, Exchange, Tick
from core.clock import combine
from storage.models import Trade
from strategies.base import Direction
from strategies.breakout_tp import (NiftyEarlyBreakoutTP, NiftyEarlyBreakoutTPSettings, NiftyRefBreakoutTP,
                                    NiftyRefBreakoutTPSettings, tp_levels)
from strategies.nifty_early_breakout import NiftyEarlyBreakoutSettings
from strategies.nifty_ref_breakout import NiftyRefBreakoutSettings, build_slots
from strategies.registry import all_templates
from tests.conftest import DAY, FakeStrategyContext, at, make_candle
from tests.engine_harness import build_env


# ---------------------------------------------------------------- settings
def test_templates_registered_and_rules_copied():
    t = all_templates()
    assert t["nifty_ref_breakout_tp"] is NiftyRefBreakoutTP and t["nifty_early_breakout_tp"] is NiftyEarlyBreakoutTP
    orig, orig_tp = NiftyRefBreakoutSettings(), NiftyRefBreakoutTPSettings()
    early, early_tp = NiftyEarlyBreakoutSettings(), NiftyEarlyBreakoutTPSettings()
    for a, b in ((orig, orig_tp), (early, early_tp)):
        assert a.reference_candle_starts == b.reference_candle_starts
        assert (a.lots, a.buffer_points, a.exit_offset_min, a.entry_window_candles) == \
               (b.lots, b.buffer_points, b.exit_offset_min, b.entry_window_candles)
        assert [s.exit_at for s in build_slots(a, DAY)] == [s.exit_at for s in build_slots(b, DAY)]
    assert (orig_tp.tp1_lots, orig_tp.tp2_lots) == (2, 2)  # 5 lots -> 2 / 2 / 1 runner
    assert (early_tp.tp1_lots, early_tp.tp2_lots) == (4, 4)  # 10 lots -> 4 / 4 / 2 runners
    assert orig_tp.tp_basis == "range_multiple" and (orig_tp.tp1_value, orig_tp.tp2_value) == (1.0, 1.25)


def test_settings_validation():
    with pytest.raises(ValidationError, match="less than Lots"):
        NiftyRefBreakoutTPSettings(tp1_lots=3, tp2_lots=2)  # 5 lots: nothing left for the timed exit
    with pytest.raises(ValidationError, match="greater than TP1"):
        NiftyRefBreakoutTPSettings(tp1_value=1.5, tp2_value=1.0)
    with pytest.raises(ValidationError, match="Take-profit mode"):
        NiftyRefBreakoutTPSettings(take_profit_mode="spot_points", take_profit_value=10)
    assert NiftyEarlyBreakoutTPSettings(lots=6, tp1_lots=2, tp2_lots=2).lots == 6


def test_switch_off():
    s = NiftyRefBreakoutTPSettings(tp_enabled=False, tp1_lots=3, tp2_lots=2)  # TP checks skipped while off
    assert s.tp_enabled is False and NiftyRefBreakoutTPSettings().tp_enabled is True


async def test_switched_off_signal_has_no_targets():
    ctx = FakeStrategyContext(start=at("09:25"))
    strat = NiftyEarlyBreakoutTP(NiftyEarlyBreakoutTPSettings(tp_enabled=False), ctx)
    await strat.on_start()
    ctx.candles[at("09:30")] = make_candle(at("09:30"), 23625, 23640.0, 23610.0, 23625)
    ctx.clock.set(at("09:35", 5))
    await strat.on_timer(ctx.now())
    ctx.clock.set(at("09:37"))
    await strat.on_tick(Tick(exchange=Exchange.NSE, token="99926000", ltp=23643.0))
    assert ctx.signals[0].lots == 10 and ctx.signals[0].exit_plan.partial_targets == []


def test_tp_levels_range_and_points():
    ref = make_candle(at("09:45"), 23620, 23640.0, 23610.0, 23620)  # range 30
    s = NiftyRefBreakoutTPSettings()
    ce = tp_levels(s, Direction.CE_BUY, 23645.0, ref)
    assert [(p.label, p.spot_level, p.side, p.lots) for p in ce] == [("tp1", 23675.0, "above", 2),
                                                                     ("tp2", 23682.5, "above", 2)]
    pe = tp_levels(s, Direction.PE_BUY, 23605.0, ref)
    assert [(p.spot_level, p.side) for p in pe] == [(23575.0, "below"), (23567.5, "below")]
    pts = tp_levels(NiftyRefBreakoutTPSettings(tp_basis="points", tp1_value=12, tp2_value=20), Direction.CE_BUY,
                    23645.0, ref)
    assert [p.spot_level for p in pts] == [23657.0, 23665.0]


async def test_signal_carries_targets():
    ctx = FakeStrategyContext(start=at("09:25"))
    strat = NiftyEarlyBreakoutTP(NiftyEarlyBreakoutTPSettings(), ctx)
    await strat.on_start()
    ctx.candles[at("09:30")] = make_candle(at("09:30"), 23625, 23640.0, 23610.0, 23625)
    ctx.clock.set(at("09:35", 5))
    await strat.on_timer(ctx.now())
    ctx.clock.set(at("09:37"))
    await strat.on_tick(Tick(exchange=Exchange.NSE, token="99926000", ltp=23643.0))
    sig = ctx.signals[0]
    assert sig.lots == 10 and sig.exit_plan.exit_at == at("09:45") and sig.exit_plan.spot_sl == 23610.0
    assert [(p.label, p.spot_level, p.lots) for p in sig.exit_plan.partial_targets] == \
           [("tp1", 23673.0, 4), ("tp2", 23680.5, 4)]


# ---------------------------------------------------------------- live engine
async def _enter(env):
    """Reference 09:45 H=23639.20 L=23609.20 (range 30) -> CE at spot 23660 -> TP1 23690, TP2 23697.5."""
    env.ref_candle("09:45", 23639.20, 23609.20)
    env.spot(23630.0)
    env.option_price(23650, "CE", 100.0)
    await env.at("09:50", 6)
    await env.at("09:52", 31)
    env.spot(23660.0)
    await env.wait_for(lambda: env.platform.trades.live)
    lt = next(iter(env.platform.trades.live.values()))
    if lt.protective_pct:
        await env.wait_for(lambda: lt.protective_sl_status == "TRIGGER_PENDING")
    return lt


def _sl_qty(env, lt) -> int:
    _, sync = env.platform.accounts[env.account_id].order_route(lt.mode)
    return sync.orders[lt.protective_sl_order_id].quantity


async def test_scale_out_tp1_tp2_then_timed_exit(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp",
                                                "params": {"protective_sl_pct": 40}}])
    try:
        lt = await _enter(env)
        assert lt.entry_qty == 325 and [p["level"] for p in lt.partials] == [23690.0, 23697.5]
        assert _sl_qty(env, lt) == 325
        env.option_price(23650, "CE", 120.0)
        env.spot(23690.0)  # TP1: 2 lots
        await env.wait_for(lambda: lt.open_qty == 195 and not lt.partial_busy)
        assert _sl_qty(env, lt) == 195  # broker stop shrunk with the position
        env.option_price(23650, "CE", 130.0)
        env.spot(23698.0)  # TP2: 2 more lots
        await env.wait_for(lambda: lt.open_qty == 65 and not lt.partial_busy)
        assert _sl_qty(env, lt) == 65
        await env.settle()
        assert lt.id in env.platform.trades.live  # last lot is still running
        env.option_price(23650, "CE", 110.0)
        await env.at("10:00", 0)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.get(Trade, lt.id)
        assert t.status == "CLOSED" and t.exit_reason == "timed_exit"
        assert t.gross_pnl == pytest.approx(20 * 130 + 30 * 130 + 10 * 65)
        assert [(p["label"], p["filled"], p["price"]) for p in t.meta["partials"]] == [("tp1", 130, 120.0),
                                                                                         ("tp2", 130, 130.0)]
        assert len(env.messages("PARTIAL_EXIT")) == 2
        assert "partials=tp1:130@120.00,tp2:130@130.00" in env.messages("EXIT")[-1]
        assert not env.types("PARTIAL_MANUAL_EXIT") and not env.types("EXIT_FAILED")
    finally:
        await env.platform.stop()


async def test_spot_sl_after_tp1_closes_the_rest(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp"}])
    try:
        lt = await _enter(env)
        env.option_price(23650, "CE", 120.0)
        env.spot(23691.0)
        await env.wait_for(lambda: lt.open_qty == 195 and not lt.partial_busy)
        env.option_price(23650, "CE", 80.0)
        env.spot(23609.0)  # spot SL (ref low) takes out the remaining 3 lots
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.get(Trade, lt.id)
        assert t.exit_reason == "spot_stop_loss"
        assert t.gross_pnl == pytest.approx(20 * 130 - 20 * 195)
    finally:
        await env.platform.stop()


async def test_original_template_never_scales_out(tmp_path):
    env = await build_env(tmp_path)  # plain nifty_ref_breakout
    try:
        lt = await _enter(env)
        assert lt.partials == []
        env.spot(23720.0)
        await env.settle()
        assert lt.open_qty == 325 and not env.types("PARTIAL_EXIT")
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- backtest
BT_DAY = date(2026, 11, 4)


def _day_with_breakout() -> list[Candle]:
    bars = {}
    t = combine(BT_DAY, time(9, 15))
    while t < combine(BT_DAY, time(15, 30)):
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23601, low=23599, close=23600)
        t += timedelta(minutes=1)
    for i in range(5):  # reference 09:45: range 20 -> TP1 +20, TP2 +25 from entry
        t = combine(BT_DAY, time(9, 45 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    t = combine(BT_DAY, time(9, 51))
    bars[t] = Candle(start=t, timeframe_min=1, open=23605, high=23614, low=23604, close=23613)  # entry ~23612.5
    for m, px in ((52, 23640), (53, 23645), (54, 23646)):  # runs through TP1 and TP2, then holds
        t = combine(BT_DAY, time(9, m))
        bars[t] = Candle(start=t, timeframe_min=1, open=px - 5, high=px, low=px - 6, close=px)
    for m in range(55, 60):
        t = combine(BT_DAY, time(9, m))
        bars[t] = Candle(start=t, timeframe_min=1, open=23646, high=23647, low=23645, close=23646)
    return list(bars.values())


async def test_backtest_scales_out_like_live():
    cfg = BacktestConfig(template_id="nifty_ref_breakout_tp", params={"reference_candle_starts": "09:45"},
                         day_from=BT_DAY, day_to=BT_DAY, slippage_pct=0)
    res = await run_backtest(cfg, _day_with_breakout())
    tr = res["trades"][0]
    assert tr["direction"] == "CE_BUY" and tr["exit_reason"] == "timed_exit"
    assert tr["partials"].startswith("tp1:130@") and ", tp2:130@" in tr["partials"]
    plain = await run_backtest(BacktestConfig(template_id="nifty_ref_breakout",
                                              params={"reference_candle_starts": "09:45"}, day_from=BT_DAY,
                                              day_to=BT_DAY, slippage_pct=0), _day_with_breakout())
    p = plain["trades"][0]
    assert p["partials"] == "" and p["qty"] == tr["qty"] == 325
    # same entry; P&L differs only because 4 of 5 lots were sold earlier
    assert p["entry_premium"] == tr["entry_premium"]
    assert tr["gross"] != p["gross"]
