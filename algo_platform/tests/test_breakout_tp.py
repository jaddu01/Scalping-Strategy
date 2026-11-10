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
    assert orig_tp.split() == (2, 2)  # 5 lots -> 2 / 2 / 1 runner
    assert early_tp.split() == (4, 4)  # 10 lots -> 4 / 4 / 2 runners
    assert orig_tp.tp_basis == "premium_pct" and (orig_tp.tp1_value, orig_tp.tp2_value) == (20.0, 30.0)
    assert orig_tp.exit_early_sec == 2


def test_automatic_lot_split():
    split = {n: NiftyRefBreakoutTPSettings(lots=n).split() for n in (3, 4, 5, 6, 8, 10, 20)}
    assert split == {3: (1, 1), 4: (1, 1), 5: (2, 2), 6: (2, 2), 8: (3, 3), 10: (4, 4), 20: (8, 8)}
    assert NiftyRefBreakoutTPSettings(lots=2).split() == (0, 0)  # too small: no scale-out, all at SL/time
    ref = make_candle(at("09:45"), 23620, 23640.0, 23610.0, 23620)
    assert tp_levels(NiftyRefBreakoutTPSettings(lots=2), Direction.CE_BUY, 23645.0, ref) == []


def test_settings_validation():
    with pytest.raises(ValidationError, match="less than Lots"):
        NiftyRefBreakoutTPSettings(tp1_lots=3, tp2_lots=2)  # 5 lots: nothing left for the timed exit
    with pytest.raises(ValidationError, match="greater than TP1"):
        NiftyRefBreakoutTPSettings(tp1_value=1.5, tp2_value=1.0)
    with pytest.raises(ValidationError, match="Take-profit mode"):
        NiftyRefBreakoutTPSettings(take_profit_mode="spot_points", take_profit_value=10)
    assert NiftyEarlyBreakoutTPSettings(lots=6, tp1_lots=2, tp2_lots=2).split() == (2, 2)
    with pytest.raises(ValidationError, match="both"):
        NiftyRefBreakoutTPSettings(tp1_lots=2, tp2_lots=0)


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


def test_tp_levels_premium_default():
    ref = make_candle(at("09:45"), 23620, 23640.0, 23610.0, 23620)
    for d in (Direction.CE_BUY, Direction.PE_BUY):  # premium targets are the same for CE and PE: price up
        got = [(p.label, p.spot_level, p.side, p.lots, p.premium_pct)
               for p in tp_levels(NiftyRefBreakoutTPSettings(), d, 23645.0, ref)]
        assert got == [("tp1", None, "above", 2, 20.0), ("tp2", None, "above", 2, 30.0)]


def test_tp_levels_range_and_points():
    ref = make_candle(at("09:45"), 23620, 23640.0, 23610.0, 23620)  # range 30
    s = NiftyRefBreakoutTPSettings(tp_basis="range_multiple", tp1_value=1.0, tp2_value=1.25)
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
    assert sig.lots == 10 and sig.exit_plan.exit_at == at("09:44", 58) and sig.exit_plan.spot_sl == 23610.0
    assert [(p.label, p.premium_pct, p.lots) for p in sig.exit_plan.partial_targets] == \
           [("tp1", 20.0, 4), ("tp2", 30.0, 4)]


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
        # premium targets from the entry fill of 100: +20 % and +30 %
        assert lt.entry_qty == 325 and [p["level"] for p in lt.partials] == [120.0, 130.0]
        assert _sl_qty(env, lt) == 325
        env.option_price(23650, "CE", 119.95)
        await env.settle()
        assert lt.open_qty == 325  # just below +20 %: nothing sold
        env.option_price(23650, "CE", 120.0)  # TP1: 2 lots
        await env.wait_for(lambda: lt.open_qty == 195 and not lt.partial_busy)
        assert _sl_qty(env, lt) == 195  # broker stop shrunk with the position
        env.option_price(23650, "CE", 130.0)  # TP2: 2 more lots
        await env.wait_for(lambda: lt.open_qty == 65 and not lt.partial_busy)
        assert _sl_qty(env, lt) == 65
        await env.settle()
        assert lt.id in env.platform.trades.live  # last lot is still running
        env.option_price(23650, "CE", 110.0)
        await env.at("09:59", 58)  # timed exit: reference start + 15 min - 2 s
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.get(Trade, lt.id)
        assert t.status == "CLOSED" and t.exit_reason == "timed_exit"
        assert t.gross_pnl == pytest.approx(20 * 130 + 30 * 130 + 10 * 65)
        assert [(p["label"], p["filled"], p["price"]) for p in t.meta["partials"]] == [("tp1", 130, 120.0),
                                                                                         ("tp2", 130, 130.0)]
        assert all(p["spot"] == 23660.0 for p in t.meta["partials"])  # spot when each TP sold
        assert t.exit_time.strftime("%H:%M:%S") == "09:59:58"
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


async def test_spot_range_basis_still_available(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp", "params": {
        "tp_basis": "range_multiple", "tp1_value": 1.0, "tp2_value": 1.25}}])
    try:
        lt = await _enter(env)
        assert [p["level"] for p in lt.partials] == [23690.0, 23697.5]  # spot: entry 23660 + 1.0x / 1.25x range 30
        env.option_price(23650, "CE", 150.0)  # premium +50 % does not matter on the spot basis
        await env.settle()
        assert lt.open_qty == 325
        env.spot(23690.0)
        await env.wait_for(lambda: lt.open_qty == 195 and not lt.partial_busy)
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
    cfg = BacktestConfig(template_id="nifty_ref_breakout_tp",
                         params={"reference_candle_starts": "09:45", "tp_basis": "range_multiple",
                                 "tp1_value": 1.0, "tp2_value": 1.25},
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


async def test_backtest_premium_targets():
    cfg = BacktestConfig(template_id="nifty_ref_breakout_tp", params={"reference_candle_starts": "09:45"},
                         day_from=BT_DAY, day_to=BT_DAY, slippage_pct=0, iv=0.13)
    res = await run_backtest(cfg, _day_with_breakout())
    tr = res["trades"][0]
    assert tr["exit_time"] == "09:59:58"
    for leg in filter(None, tr["partials"].split(", ")):
        label, rest = leg.split(":")
        price = float(rest.split("@")[1])
        need = 1.2 if label == "tp1" else 1.3
        assert price >= tr["entry_premium"] * need - 0.01  # sold only once the modelled premium was up 20 / 30 %


async def test_slots_route_shows_trade_points_and_tp_legs(tmp_path):
    """The strategy page's slots table, fed from the real engine: open trade (live MTM), then closed."""
    import httpx
    from fastapi import FastAPI

    from app.auth.deps import current_user
    from app.web import routes_strategies

    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp"}])
    try:
        app = FastAPI()
        app.state.platform, app.state.db = env.platform, env.db
        app.include_router(routes_strategies.router)
        app.dependency_overrides[current_user] = lambda: None
        iid = env.instance_ids[0]
        lt = await _enter(env)
        env.option_price(23650, "CE", 120.0)  # TP1
        await env.wait_for(lambda: lt.open_qty == 195 and not lt.partial_busy)
        env.spot(23670.0)
        await env.settle()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            html = (await c.get(f"/strategies/{iid}/slots")).text
            assert "23,660.00" in html and "23,670.00" in html and "live" in html and "MTM" in html
            assert "TP1 2 lots @ spot 23,660.00 (+0.00 pts), premium 120.00" in html
            env.option_price(23650, "CE", 110.0)
            await env.at("09:59", 58)
            await env.wait_for(lambda: not env.platform.trades.live)
            html = (await c.get(f"/strategies/{iid}/slots")).text
            # entry 23660 -> TP1 2 lots at 23660 (0 pts) + 3 lots out at 23670 (+10) = +6.00 weighted
            assert "+6.00" in html and "100.00 → 114.00" in html and "Today: 1 trade" in html
            assert "09:59:58" in html
    finally:
        await env.platform.stop()
