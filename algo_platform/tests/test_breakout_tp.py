"""TP1 / TP2 variants: settings, levels, live engine scale-out (with protective SL resizing) and backtest."""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backtest.engine import BacktestConfig, run_backtest
from brokers.base import Candle, Exchange, Tick
from core.clock import combine
from storage.models import OrderRecord as OrderRecordM, Trade
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
        from app.auth.security import SessionInfo

        app.dependency_overrides[current_user] = lambda: SessionInfo("", 1, "admin", "admin", "", False, False)
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


# ---------------------------------------------------------------- 4 TPs by entry premium
def test_band_pct_edges():
    from engine.trades import band_pct

    s = NiftyRefBreakoutTPSettings(tp_mode="four_by_premium")
    bands = [s.bands(i) for i in range(4)]
    assert [band_pct(bands[0], p) for p in (10, 79.95, 80, 150, 150.05, 900)] == [20, 20, 15, 15, 8, 8]
    assert [band_pct(b, 64.8) for b in bands] == [20, 30, 40, 50]
    assert [band_pct(b, 120) for b in bands] == [15, 22, 30, 40]
    assert [band_pct(b, 200) for b in bands] == [8, 12, 20, 25]


def test_four_tp_settings_checks_and_levels():
    from strategies.nifty_two_entry import NiftyTwoEntryTPSettings

    s = NiftyTwoEntryTPSettings(lots=10, tp_mode="four_by_premium")
    assert s.split4() == (3, 3, 2, 2)
    # a run that still has its two-target lots (4 / 4) switches to 4 TPs with the automatic split
    assert NiftyTwoEntryTPSettings(lots=10, tp_mode="four_by_premium", tp1_lots=4, tp2_lots=4).split4() == (3, 3, 2, 2)
    assert NiftyTwoEntryTPSettings(lots=10, tp_mode="four_by_premium", tp1_lots=2, tp2_lots=2, tp3_lots=2,
                                   tp4_lots=2).split4() == (2, 2, 2, 2)
    pts = tp_levels(s, Direction.CE_BUY, 23660, make_candle(at("09:45"), 1, 2, 0, 1))
    assert [(p.label, p.lots, p.premium_pct, p.premium_bands[0]) for p in pts] == [
        ("tp1", 3, None, (80.0, 20.0)), ("tp2", 3, None, (80.0, 30.0)), ("tp3", 2, None, (80.0, 40.0)),
        ("tp4", 2, None, (80.0, 50.0))]
    # two-target mode is unchanged
    assert [p.label for p in tp_levels(NiftyTwoEntryTPSettings(lots=10), Direction.CE_BUY, 23660,
                                       make_candle(at("09:45"), 1, 2, 0, 1))] == ["tp1", "tp2"]
    for kw, msg in (({"tp_pcts_low": [20, 30, 40]}, "needs 4 percentages"),
                    ({"tp_pcts_mid": [15, 22, 22, 40]}, "must be above 0 and rise"),
                    ({"tp_band2_max": 60}, "middle band"),
                    ({"tp3_lots": 2, "tp4_lots": 2}, "set each of TP1-TP4 lots"),
                    ({"tp1_lots": 4, "tp2_lots": 4, "tp3_lots": 2}, "set each of TP1-TP4 lots"),
                    ({"tp1_lots": 4, "tp2_lots": 4, "tp3_lots": 2, "tp4_lots": 2}, "more than Lots"),
                    ({"tp_basis": "points"}, "premium_pct")):
        with pytest.raises(ValidationError, match=msg):
            NiftyTwoEntryTPSettings(lots=10, tp_mode="four_by_premium", **kw)


async def test_four_tps_by_premium_band_sell_the_whole_position(tmp_path):
    """Entry fill 100 is in the 80-150 band: 15 / 22 / 30 / 40 % -> 115 / 122 / 130 / 140; 3/3/2/2 of 10 lots;
    TP4 sells the last lots and closes the trade (exit reason tp4); the broker stop shrinks and is cancelled."""
    env = await build_env(tmp_path, instances=[{"name": "TP4", "template": "nifty_ref_breakout_tp", "params": {
        "protective_sl_pct": 40, "lots": 10, "tp_mode": "four_by_premium"}}])
    try:
        lt = await _enter(env)
        assert lt.entry_qty == 650
        assert [(p["label"], p["pct"], p["level"], p["qty"]) for p in lt.partials] == [
            ("tp1", 15.0, 115.0, 195), ("tp2", 22.0, 122.0, 195), ("tp3", 30.0, 130.0, 130), ("tp4", 40.0, 140.0, 130)]
        for price, left in ((115.0, 455), (122.0, 260), (130.0, 130)):
            env.option_price(23650, "CE", price)
            await env.wait_for(lambda left=left: lt.open_qty == left and not lt.partial_busy)
            assert _sl_qty(env, lt) == left
        env.option_price(23650, "CE", 140.0)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.get(Trade, lt.id)
        assert t.status == "CLOSED" and t.exit_reason == "tp4" and t.open_quantity == 0
        assert t.gross_pnl == pytest.approx(15 * 195 + 22 * 195 + 30 * 130 + 40 * 130)
        assert [(p["label"], p["filled"], p["price"]) for p in t.meta["partials"]] == [
            ("tp1", 195, 115.0), ("tp2", 195, 122.0), ("tp3", 130, 130.0), ("tp4", 130, 140.0)]
        assert not env.types("EXIT_FAILED") and not env.types("PARTIAL_EXIT_FAILED")
    finally:
        await env.platform.stop()


async def test_four_tps_spot_sl_after_tp2_closes_the_rest(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP4", "template": "nifty_ref_breakout_tp", "params": {
        "lots": 10, "tp_mode": "four_by_premium"}}])
    try:
        lt = await _enter(env)
        env.option_price(23650, "CE", 122.0)  # TP1 and TP2 levels are both passed
        await env.wait_for(lambda: lt.open_qty == 260 and not lt.partial_busy)
        env.option_price(23650, "CE", 80.0)
        env.spot(23609.0)  # spot SL takes out the 4 lots left for TP3 / TP4
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.get(Trade, lt.id)
        assert t.exit_reason == "spot_stop_loss"
        assert [p["filled"] for p in t.meta["partials"]] == [195, 195, 0, 0]
        assert t.gross_pnl == pytest.approx(22 * 195 + 22 * 195 - 20 * 260)
    finally:
        await env.platform.stop()


async def test_tp_partial_waits_while_a_manual_stop_holds_the_position(tmp_path):
    """Live: a manual SELL order at the broker holds the lots; a TP sell on top would be a short sale (rejected
    for funds). No order, one alert, and the TP sells once the manual order is cancelled."""
    from brokers.base import OrderRequest, OrderType, Side

    env = await build_env(tmp_path, mode="live", instances=[{"name": "TP", "template": "nifty_ref_breakout_tp"}])
    try:
        tm = env.platform.trades
        tm.partial_rearm_sec = 0.05
        lt = await _enter(env)
        sync = env.platform.accounts[env.account_id].live_sync
        inst = env.mock.find_instrument(Exchange.NFO, lt.token)
        manual = await env.mock.place_order(OrderRequest(instrument=inst, side=Side.SELL, quantity=lt.open_qty,
                                                         order_type=OrderType.SL_M, trigger_price=80.0))
        await sync.poll_once(full=True)
        await env.settle()
        env.option_price(23650, "CE", 120.0)  # TP1 reached
        await env.settle(10)
        assert lt.open_qty == 325 and not lt.partials[0]["filled"]
        msgs = env.messages("EXIT_BLOCKED_BY_MANUAL_ORDER")
        assert len(msgs) == 1 and msgs[0].startswith("tp1:") and manual in msgs[0]
        with env.db.session() as s:
            assert not list(s.scalars(select(OrderRecordM).where(OrderRecordM.purpose == "EXIT")))
        await env.mock.cancel_order(manual)  # the manual stop is cancelled; the target is re-armed
        await sync.poll_once(full=True)
        env.option_price(23650, "CE", 121.0)
        await env.wait_for(lambda: lt.open_qty == 195 and not lt.partial_busy)
        assert lt.partials[0]["filled"] == 130 and len(env.messages("EXIT_BLOCKED_BY_MANUAL_ORDER")) == 1
    finally:
        await env.platform.stop()


def test_tp_choices_are_each_runs_own_setting():
    """Lots and the whole TP setup (on/off, 2 or 4 targets, % levels, lots per target) are set on each run;
    the shared rules (entries, stops, schedule) cannot overwrite them."""
    from app.web.forms import build_fields
    from app.web.shared import check_runs, merged, split_fields
    from strategies.nifty_two_entry import NiftyTwoEntryTPSettings as M

    rules = M(lots=10).model_dump(mode="json")  # rules: two targets
    own_4 = {"lots": 5, "tp_mode": "four_by_premium", "tp_pcts_high": [15, 22, 30, 40]}
    own_2 = {"lots": 10, "tp1_lots": 4, "tp2_lots": 4}
    a = M.model_validate(merged(M, rules, own_4))
    b = M.model_validate(merged(M, rules, own_2))
    assert (a.tp_mode, a.lots, a.split4(), a.tp_pcts_high) == ("four_by_premium", 5, (1, 1, 1, 1), [15, 22, 30, 40])
    assert (b.tp_mode, b.split()) == ("two_targets", (4, 4))
    # new rules (another buffer) keep each run's own TP choice
    ok, problems = check_runs(M, {**rules, "buffer_points": 3.0, "tp_mode": "two_targets"},
                              [("A", own_4), ("B", own_2)])
    assert not problems and ok["A"]["tp_mode"] == "four_by_premium" and ok["A"]["buffer_points"] == 3.0
    # the run form shows the TP fields, the rules form does not
    fields = build_fields(M, rules, {}, "params.")
    run_names = {f.name.split(".", 1)[-1] for f in split_fields(fields, personal=True)}
    rule_names = {f.name.split(".", 1)[-1] for f in split_fields(fields, personal=False)}
    assert {"lots", "tp_mode", "tp_pcts_low", "tp1_lots", "tp4_lots", "tp_enabled"} <= run_names
    assert not {"tp_mode", "tp_pcts_low", "lots"} & rule_names and "buffer_points" in rule_names
