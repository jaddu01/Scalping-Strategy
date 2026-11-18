"""Runner protection for the TP breakout strategies: a premium floor under the lots left after the start TP fills.

Replays of the two real trades that gave their gains back (18-Nov slot 1, 17-Nov slot 2), the pure floor rules
shared by the live engine and the backtest, restart persistence, priorities, settings checks and the backtest."""
from __future__ import annotations

import asyncio
from datetime import date, time, timedelta

import pytest
from pydantic import ValidationError

from backtest.engine import BacktestConfig, run_backtest
from brokers.base import Candle
from core.clock import combine
from engine.exit_manager import (ExitState, RunnerState, evaluate_exit, runner_from_plan, runner_hit, runner_on_fill,
                                 runner_on_premium)
from storage.models import Trade
from strategies.breakout_tp import NiftyRefBreakoutTPSettings
from strategies.nifty_two_entry import NiftyTwoEntryTPSettings
from tests.conftest import at
from tests.engine_harness import Env, build_env

# 18-Nov slot 1: four_by_premium, high band 8/12/20/25 %, 10 lots 3/3/2/2, entry 176.35
TRADE1 = {"lots": 10, "tp_mode": "four_by_premium", "runner_protect": True}
# 17-Nov slot 2: two_targets +20 % / +30 %, 10 lots 4/4/2, entry 34.70
TRADE2 = {"lots": 10, "tp_mode": "two_targets", "tp1_value": 20.0, "tp2_value": 30.0, "runner_protect": True}
TRADE2_POINTS = (48.50, 42.05, 54.70, 69.20, 81.95, 70.85, 93.35, 88.35, 83.20, 75.75, 72.50)


async def _enter(env: Env, premium: float):
    """Reference 09:45 H=23639.20 L=23609.20 -> CE 23650 at spot 23660, entry fill = `premium`."""
    env.ref_candle("09:45", 23639.20, 23609.20)
    env.spot(23630.0)
    env.option_price(23650, "CE", premium)
    await env.at("09:50", 6)
    await env.at("09:52", 31)
    env.spot(23660.0)
    await env.wait_for(lambda: env.platform.trades.live)
    return next(iter(env.platform.trades.live.values()))


async def _tick(env: Env, lt, price: float, left: int | None = None) -> None:
    env.option_price(23650, "CE", price)
    if left is not None:
        await env.wait_for(lambda: lt.open_qty == left and not lt.partial_busy)
    await env.settle(3)


def _row(env: Env, lt) -> Trade:
    with env.db.session() as s:
        return s.get(Trade, lt.id)


def _plan(**kw):
    return {"start": "tp2", "step": "one_behind", "band1_max": 80.0, "band2_max": 150.0,
            "trail_pcts": [20.0, 15.0, 10.0], **kw}


# ---------------------------------------------------------------- 1. off by default
def test_off_by_default():
    for s in (NiftyRefBreakoutTPSettings(), NiftyTwoEntryTPSettings(), NiftyTwoEntryTPSettings(tp_mode="four_by_premium")):
        assert s.runner_protect is False and s.runner_plan() is None
        assert (s.runner_start, s.runner_step_floor) == ("auto", "one_behind")
        assert (s.runner_trail_pct_low, s.runner_trail_pct_mid, s.runner_trail_pct_high) == (20.0, 15.0, 10.0)
    assert NiftyTwoEntryTPSettings().split() == (4, 4)  # splits unchanged
    assert NiftyTwoEntryTPSettings(tp_mode="four_by_premium").split4() == (3, 3, 2, 2)


async def test_off_trade_has_no_runner_and_exits_as_before(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp",
                                                "params": {"lots": 10, "tp1_value": 20.0, "tp2_value": 30.0}}])
    try:
        lt = await _enter(env, 34.70)
        assert lt.runner is None and env.platform.exits.states[lt.id].runner is None
        await _tick(env, lt, 41.65, 390)
        await _tick(env, lt, 45.15, 130)
        for px in TRADE2_POINTS:
            await _tick(env, lt, px)
        assert lt.id in env.platform.trades.live  # nothing but the spot SL / timed exit closes the runner
        assert env.platform.trades.live_view()[0]["runner"] is None
        await env.at("09:59", 58)
        await env.wait_for(lambda: not env.platform.trades.live)
        t = _row(env, lt)
        assert t.exit_reason == "timed_exit" and "runner" not in t.meta and "runner_plan" not in t.meta
        assert not env.types("RUNNER_FLOOR_ARMED")
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- 2. trade 1 replay (18-Nov)
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_trade1_replay_exits_on_runner_floor_not_spot_sl(tmp_path, mode):
    env = await build_env(tmp_path, mode=mode, instances=[{"name": "TP4", "template": "nifty_ref_breakout_tp",
                                                           "params": TRADE1}])
    try:
        lt = await _enter(env, 176.35)
        assert [p["level"] for p in lt.partials] == [190.50, 197.55, 211.65, 220.45]
        assert lt.runner.start == "tp3" and lt.runner.trail_pct == 10.0  # high band
        await _tick(env, lt, 190.50, 455)
        await _tick(env, lt, 197.55, 260)
        assert not lt.runner.active
        assert env.platform.trades.live_view()[0]["runner"] == "Runner floor: waiting for TP3"
        await _tick(env, lt, 211.65, 130)
        assert lt.runner.active and lt.runner.step_floor == 197.55 and lt.runner.floor == 197.55
        assert env.messages("RUNNER_FLOOR_ARMED") == [
            "start=tp3 step_floor=197.55 trail=10% peak=211.65 floor=197.55 qty=130"]
        await _tick(env, lt, 213.85)
        # max(197.55, 213.85 x 0.90 = 192.465) = 197.55
        assert (lt.runner.peak, lt.runner.floor) == (213.85, 197.55)
        assert env.platform.trades.live_view()[0]["runner"] == "Runner floor 197.55 (peak 213.85)"
        for px in (202.50, 200.00):
            await _tick(env, lt, px)
            assert lt.id in env.platform.trades.live
        await _tick(env, lt, 194.90)
        await env.wait_for(lambda: not env.platform.trades.live)
        t = _row(env, lt)
        assert t.exit_reason == "runner_floor_fallback_tp2"  # the TP2 level set the floor
        assert [p["filled"] for p in t.meta["partials"]] == [195, 195, 130, 0]  # TP4 never reached
        assert t.gross_pnl == pytest.approx((190.50 - 176.35) * 195 + (197.55 - 176.35) * 195
                                            + (211.65 - 176.35) * 130 + (194.90 - 176.35) * 130)
        exit_line = env.messages("EXIT")[-1]
        assert "reason=runner_floor_fallback_tp2" in exit_line and "floor=197.55 peak=213.85" in exit_line
        assert "reason=runner_floor_fallback_tp2" in env.messages("TRIGGERED")[-1]
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- 3. trade 2 replay (17-Nov) + 10. order label
async def test_trade2_replay_trails_the_peak(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp", "params": TRADE2}])
    try:
        lt = await _enter(env, 34.70)
        assert [p["level"] for p in lt.partials] == [41.65, 45.15]
        assert lt.runner.start == "tp2" and lt.runner.trail_pct == 20.0  # low band
        await _tick(env, lt, 41.65, 390)
        assert not lt.runner.active
        await _tick(env, lt, 45.15, 130)
        assert lt.runner.active and lt.runner.floor == 41.65  # step = TP1 level; 45.15 x 0.8 = 36.12
        floors = {}
        for px in TRADE2_POINTS[:-1]:
            await _tick(env, lt, px)
            assert lt.id in env.platform.trades.live, f"{px} must not exit"
            floors[px] = lt.runner.floor
        assert floors[42.05] == 41.65 and floors[70.85] == 65.55  # 42.05 and 70.85 stay above the floor
        assert lt.runner.peak == 93.35 and lt.runner.floor == 74.65  # 93.35 x 0.80 = 74.68 -> 74.65
        await _tick(env, lt, 72.50)
        await env.wait_for(lambda: not env.platform.trades.live)
        t = _row(env, lt)
        assert t.exit_reason == "runner_floor_trail"  # 93.35 x 0.80 is above the TP1 step
        assert t.gross_pnl == pytest.approx((41.65 - 34.70) * 260 + (45.15 - 34.70) * 260 + (72.50 - 34.70) * 130)
        # the RAISED lines are throttled (>= 0.5 % or a new TP), not one per tick
        assert 1 <= len(env.types("RUNNER_FLOOR_RAISED")) <= len(TRADE2_POINTS)
        # 10. the final exit after partial sells labels the remaining lots, not the trade's original 10
        reqs = [m for m in env.messages("REQUEST") if "purpose=EXIT" in m]
        assert ["qty=4 lots (260 units)" in reqs[0], "qty=4 lots (260 units)" in reqs[1],
                "qty=2 lots (130 units)" in reqs[2]] == [True, True, True]
        assert not any("10 lots (130 units)" in m for m in reqs)
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- 4. never down, also across a restart
def test_floor_never_moves_down_pure():
    r = runner_from_plan(_plan(), 34.70)
    partials = [{"label": "tp1", "level": 41.65, "filled": 260, "price": 41.65},
                {"label": "tp2", "level": 45.15, "filled": 260, "price": 45.15}]
    assert runner_on_fill(r, partials, 34.70) == "armed"
    seen = []
    for px in (60.0, 55.0, 80.0, 40.0, 79.0, 100.0, 50.0):
        runner_on_premium(r, px)
        seen.append(r.floor)
    assert seen == sorted(seen) and r.floor == 80.0  # 100 x 0.8
    back = RunnerState.from_dict(r.as_dict())
    runner_on_premium(back, 90.0)  # lower peak after a restart
    assert (back.peak, back.floor) == (100.0, 80.0)


async def test_floor_and_peak_survive_a_restart(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp", "params": TRADE2}])
    try:
        lt = await _enter(env, 34.70)
        await _tick(env, lt, 41.65, 390)
        await _tick(env, lt, 45.15, 130)
        await _tick(env, lt, 93.35)
        assert lt.runner.floor == 74.65
        paper_state = env.platform.accounts[env.account_id].paper_shadow.snapshot()
        assert _row(env, lt).meta["runner"]["floor"] == 74.65  # persisted on every floor change
    finally:
        await env.platform.stop()
    from core.config import AppConfig
    from engine.platform import Platform
    from storage.repo import save_paper_state

    save_paper_state(env.db, f"shadow:{env.account_id}", paper_state)
    p2 = Platform(AppConfig(data_dir=tmp_path, database_url=env.db.url), env.db, clock=env.clock, db_events=False,
                  file_events=False)
    events: list = []
    p2.bus.add_sink(events.append)
    await p2.start()
    env2 = Env(platform=p2, db=env.db, clock=env.clock, events=events, account_id=env.account_id,
               instance_ids=env.instance_ids)
    try:
        await env2.wait_for(lambda: p2.accounts[env.account_id].ready.is_set() and lt.id in p2.trades.live)
        lt2 = p2.trades.live[lt.id]
        assert lt2.runner.active and (lt2.runner.peak, lt2.runner.floor) == (93.35, 74.65)
        assert p2.exits.states[lt.id].runner is lt2.runner
        await _tick(env2, lt2, 80.0)  # lower peak: the floor stays
        assert lt2.runner.floor == 74.65 and lt.id in p2.trades.live
        await _tick(env2, lt2, 74.60)
        await env2.wait_for(lambda: not p2.trades.live)
        assert _row(env2, lt).exit_reason == "runner_floor_trail"
    finally:
        await p2.stop()


# ---------------------------------------------------------------- 5. start TP never fills
async def test_no_floor_until_the_start_tp_fills(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP4", "template": "nifty_ref_breakout_tp", "params": TRADE1}])
    try:
        lt = await _enter(env, 176.35)
        await _tick(env, lt, 190.50, 455)
        await _tick(env, lt, 197.55, 260)
        for px in (205.0, 160.0, 154.0):  # would be under any floor, but TP3 never filled
            await _tick(env, lt, px)
        assert lt.id in env.platform.trades.live and not lt.runner.active
        env.spot(23609.0)  # the spot SL closes the rest as before
        await env.wait_for(lambda: not env.platform.trades.live)
        assert _row(env, lt).exit_reason == "spot_stop_loss" and not env.types("RUNNER_FLOOR_ARMED")
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- 6. spot SL wins on the same tick
def test_spot_sl_wins_over_the_floor():
    from brokers.base import Exchange, InstrumentKey, InstrumentType

    r = RunnerState(start="tp2", active=True, peak=100.0, floor=80.0)
    st = ExitState(trade_db_id=1, trade_ref="T", instance_id=None, instance_name="x", account_id=1, mode="paper",
                   direction="CE_BUY", spot_series="s", option_series="o",
                   option_key=InstrumentKey(underlying="NIFTY", exchange=Exchange.NFO, instrument_type=InstrumentType.CE),
                   spot_key=None, entry_price=50.0, quantity=130, exit_at=at("15:15"), spot_sl=23600.0, runner=r)
    assert evaluate_exit(st, 23599.0, 79.0, at("10:00")) == "spot_stop_loss"
    assert evaluate_exit(st, 23650.0, 79.0, at("10:00")) == "runner_floor_trail"
    assert evaluate_exit(st, 23650.0, 80.05, at("10:00")) is None
    st.tp_premium_level = 70.0  # the floor is checked before a take-profit
    assert evaluate_exit(st, 23650.0, 79.0, at("10:00")) == "runner_floor_trail"


async def test_spot_sl_wins_live(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp", "params": TRADE2}])
    try:
        lt = await _enter(env, 34.70)
        await _tick(env, lt, 41.65, 390)
        await _tick(env, lt, 45.15, 130)
        env.spot(23609.0)  # spot SL and floor (41.65) on the same tick
        env.option_price(23650, "CE", 40.0)
        await env.wait_for(lambda: not env.platform.trades.live)
        assert _row(env, lt).exit_reason == "spot_stop_loss"
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- 7. triggered / rejected / timed out
def test_unfilled_start_tp_does_not_arm():
    r = runner_from_plan(_plan(), 34.70)
    partials = [{"label": "tp1", "level": 41.65, "filled": 260, "price": 41.65},
                {"label": "tp2", "level": 45.15, "done": True, "filled": 0, "price": None}]
    assert runner_on_fill(r, partials, 34.70) is None and not r.active
    assert not runner_hit(r, 1.0)


async def test_start_tp_in_flight_is_not_armed(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp", "params": TRADE2}])
    om = env.platform.trades.orders
    real_wait, gate = om.wait_terminal, asyncio.Event()

    async def slow_wait(sync, oid, timeout):
        await gate.wait()
        return await real_wait(sync, oid, timeout)

    try:
        lt = await _enter(env, 34.70)
        await _tick(env, lt, 41.65, 390)
        om.wait_terminal = slow_wait
        env.option_price(23650, "CE", 45.15)
        await env.wait_for(lambda: lt.partial_busy)
        await _tick(env, lt, 42.0)  # TP2 triggered, sell not confirmed yet: no floor
        assert not lt.runner.active and lt.id in env.platform.trades.live
        gate.set()
        await env.wait_for(lambda: not lt.partial_busy and lt.open_qty == 130)
        assert lt.runner.active
    finally:
        om.wait_terminal = real_wait
        await env.platform.stop()


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
async def test_failed_start_tp_sell_does_not_arm(tmp_path, failure):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp", "params": TRADE2}])
    om = env.platform.trades.orders
    real_place, real_wait, real_cancel = om.place, om.wait_terminal, om.cancel
    try:
        lt = await _enter(env, 34.70)
        await _tick(env, lt, 41.65, 390)
        if failure == "rejected":
            from engine.order_manager import PlacedOrder

            async def place(adapter, sync, req, **kw):
                if kw.get("purpose") == "EXIT":
                    return PlacedOrder(order_id=None, record_id=None, error="rejected")
                return await real_place(adapter, sync, req, **kw)
            om.place = place
        else:
            async def wait(sync, oid, timeout):
                return None

            async def cancel(*a, **kw):
                return None
            om.wait_terminal, om.cancel = wait, cancel
        env.option_price(23650, "CE", 45.15)
        await env.wait_for(lambda: env.types("PARTIAL_EXIT_FAILED"))
        await env.settle()
        assert lt.partials[1]["filled"] == 0 and not lt.runner.active
        assert not env.types("RUNNER_FLOOR_ARMED")
    finally:
        om.place, om.wait_terminal, om.cancel = real_place, real_wait, real_cancel
        await env.platform.stop()


# ---------------------------------------------------------------- 8. settings checks
@pytest.mark.parametrize("kw,msg", [
    ({"tp_basis": "range_multiple", "tp1_value": 1.0, "tp2_value": 1.25}, "premium_pct"),
    ({"tp_basis": "points"}, "premium_pct"),
    ({"tp_enabled": False}, "Use TP1 / TP2"),
    ({"runner_start": "tp3"}, "only TP1-TP2"),
    ({"lots": 2}, "TP2 gets no lots"),  # automatic 0/0 split
    ({"lots": 5, "tp1_lots": 3, "tp2_lots": 1, "runner_start": "tp1", "runner_step_floor": "off",
      "runner_trail_pct_low": 0, "runner_trail_pct_mid": 0, "runner_trail_pct_high": 0}, "trail %"),
])
def test_settings_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        NiftyTwoEntryTPSettings(**{"runner_protect": True, **kw})


def test_settings_validation_four_tps():
    with pytest.raises(ValidationError, match="no lot is left after TP3"):  # own split 4/3/3/0 leaves nothing
        NiftyTwoEntryTPSettings(runner_protect=True, lots=10, tp_mode="four_by_premium", tp1_lots=4, tp2_lots=3,
                                tp3_lots=3, tp4_lots=1)
    with pytest.raises(ValidationError, match="TP3 gets no lots"):  # 2 lots: 1/1/0/0
        NiftyTwoEntryTPSettings(runner_protect=True, lots=2, tp_mode="four_by_premium")
    ok = NiftyTwoEntryTPSettings(runner_protect=True, lots=10, tp_mode="four_by_premium")
    assert ok.runner_plan() == {"start": "tp3", "step": "one_behind", "band1_max": 80.0, "band2_max": 150.0,
                                "trail_pcts": [20.0, 15.0, 10.0], "buffer_pct": 0.0, "timeout_min": 0}
    assert NiftyTwoEntryTPSettings(runner_protect=True).runner_plan()["start"] == "tp2"
    assert NiftyTwoEntryTPSettings(runner_protect=True, runner_start="tp1").runner_plan()["start"] == "tp1"


def test_trail_band_from_entry_fill_and_step_one_behind():
    assert [runner_from_plan(_plan(), e).trail_pct for e in (79.95, 80.0, 150.0, 150.05)] == [20, 15, 15, 10]
    r = runner_from_plan(_plan(start="tp1"), 100.0)
    parts = [{"label": "tp1", "level": 115.0, "filled": 195, "price": 115.2},
             {"label": "tp2", "level": 122.0, "filled": 0, "price": None}]
    assert runner_on_fill(r, parts, 100.0) == "armed"
    assert (r.peak, r.step_floor, r.floor) == (115.2, 100.0, 100.0)  # entry fill; 115.2 x 0.85 = 97.92
    parts[1].update(filled=195, price=122.1)
    assert runner_on_fill(r, parts, 100.0) == "raised" and r.floor == 115.0  # step moves up to TP1's level
    r2 = runner_from_plan(_plan(step="off"), 100.0)
    runner_on_fill(r2, parts, 100.0)
    assert r2.step_floor is None and r2.floor == 103.75  # 122.1 x 0.85 = 103.785 -> rounded down


# ---------------------------------------------------------------- 9. backtest
BT_DAY = date(2026, 11, 4)


def _runner_day() -> list[Candle]:
    """Ref 09:45 range 20; CE entry ~23612.5; rally through TP1 / TP2 to a peak, then back to 23620 (far above the
    spot SL 23590) and flat until the timed exit."""
    bars = {}
    t = combine(BT_DAY, time(9, 15))
    while t < combine(BT_DAY, time(15, 30)):
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23601, low=23599, close=23600)
        t += timedelta(minutes=1)
    for i in range(5):
        t = combine(BT_DAY, time(9, 45 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    t = combine(BT_DAY, time(9, 51))
    bars[t] = Candle(start=t, timeframe_min=1, open=23605, high=23614, low=23604, close=23613)
    for m, px in ((52, 23660), (53, 23700), (54, 23740)):
        t = combine(BT_DAY, time(9, m))
        bars[t] = Candle(start=t, timeframe_min=1, open=px - 30, high=px, low=px - 31, close=px)
    for m in range(55, 60):
        t = combine(BT_DAY, time(9, m))
        bars[t] = Candle(start=t, timeframe_min=1, open=23625, high=23626, low=23620, close=23620)
    return list(bars.values())


async def test_backtest_runner_on_off():
    base = {"reference_candle_starts": "09:45", "lots": 10, "tp1_value": 20.0, "tp2_value": 30.0}
    off = await run_backtest(BacktestConfig(template_id="nifty_ref_breakout_tp", params=base, day_from=BT_DAY,
                                            day_to=BT_DAY, slippage_pct=0), _runner_day())
    on = await run_backtest(BacktestConfig(template_id="nifty_ref_breakout_tp", params={**base, "runner_protect": True},
                                           day_from=BT_DAY, day_to=BT_DAY, slippage_pct=0), _runner_day())
    a, b = off["trades"][0], on["trades"][0]
    assert a["exit_reason"] == "timed_exit" and b["exit_reason"].startswith("runner_floor_")
    assert a["partials"] == b["partials"] and a["entry_premium"] == b["entry_premium"]
    assert b["gross"] > a["gross"]
    # the backtest used the same rules as live: replay its premiums through the shared functions
    armed = next(e["message"] for e in on["events"] if e["type"] == "RUNNER_FLOOR_ARMED")
    exit_line = next(e["message"] for e in on["events"] if e["type"] == "EXIT")
    assert armed.startswith("start=tp2 ") and "floor=" in exit_line and "peak=" in exit_line
    floor = float(exit_line.split("floor=")[1].split()[0])
    peak = float(exit_line.split("peak=")[1].split()[0])
    r = runner_from_plan(_plan(), b["entry_premium"])
    tp1_level = round(b["entry_premium"] * 1.2, 2)
    r.active, r.peak, r.step_floor = True, peak, tp1_level
    runner_on_premium(r, peak + 0.0)  # recompute at the recorded peak
    from engine.exit_manager import _runner_recompute

    _runner_recompute(r)
    assert r.floor == floor
    last_sell = (b["exit_premium"] * b["qty"] - sum(
        int(p.split(":")[1].split("@")[0]) * float(p.split("@")[1]) for p in b["partials"].split(", "))) / 130
    assert last_sell <= floor  # sold on the first modelled premium at or below the floor


# ---------------------------------------------------------------- fallback to TP1, buffer %, timeout after TP1
def _armed(entry=176.35, **plan):
    r = runner_from_plan(_plan(start="tp3", **plan), entry)
    parts = [{"label": "tp1", "level": 190.50, "filled": 195, "price": 190.50},
             {"label": "tp2", "level": 197.55, "filled": 195, "price": 197.55},
             {"label": "tp3", "level": 211.65, "filled": 130, "price": 211.65},
             {"label": "tp4", "level": 220.45, "filled": 0, "price": None}]
    assert runner_on_fill(r, parts, entry, at("10:00")) == "armed"
    return r, parts


def test_step_floor_tp1_falls_back_to_tp1():
    r, _ = _armed(step="tp1", trail_pcts=[0, 0, 0])
    assert (r.step_label, r.floor) == ("tp1", 190.50)
    assert not runner_hit(r, 195.0) and runner_hit(r, 190.50)
    assert r.floor_reason() == "runner_floor_fallback_tp1"
    r2, _ = _armed(trail_pcts=[0, 0, 0])  # one_behind after TP3 -> TP2
    assert (r2.floor, r2.floor_reason()) == (197.55, "runner_floor_fallback_tp2")


def test_reason_follows_the_part_that_set_the_floor():
    r, _ = _armed()  # 10 % trail, step 197.55
    assert r.floor_reason() == "runner_floor_fallback_tp2"
    runner_on_premium(r, 230.0)  # 230 x 0.9 = 207 > 197.55
    assert (r.floor, r.floor_reason()) == (207.0, "runner_floor_trail")


def test_buffer_pct_exits_a_little_below_the_floor():
    r, _ = _armed(trail_pcts=[0, 0, 0], buffer_pct=0.5)
    assert r.floor == 197.55 and r.exit_level == 196.55  # 197.55 x 0.995 = 196.562 -> rounded down
    assert not runner_hit(r, 197.0)  # between the exit level and the floor: no exit
    assert runner_hit(r, 196.55)
    assert r.text() == "Runner floor 197.55 (exit ≤ 196.55, peak 211.65)"


def test_timeout_counts_from_tp1_fill():
    r = runner_from_plan(_plan(start="tp3", timeout_min=20), 176.35)
    parts = [{"label": "tp1", "level": 190.50, "filled": 195, "price": 190.50},
             {"label": "tp2", "level": 197.55, "filled": 0, "price": None},
             {"label": "tp3", "level": 211.65, "filled": 0, "price": None}]
    runner_on_fill(r, parts, 176.35, at("09:53", 58))  # TP1: clock starts, floor not on yet
    assert r.tp1_at == at("09:53", 58) and not r.active and not r_timed(r, at("10:30"))
    parts[1].update(filled=195, price=197.55)
    runner_on_fill(r, parts, 176.35, at("09:57"))
    parts[2].update(filled=130, price=211.65)
    assert runner_on_fill(r, parts, 176.35, at("10:03")) == "armed"
    assert r.tp1_at == at("09:53", 58) and r.timeout_at == at("10:13", 58)  # not reset by later TPs
    assert not r_timed(r, at("10:13", 57)) and r_timed(r, at("10:13", 58))
    back = RunnerState.from_dict(r.as_dict())  # restart keeps the clock
    assert back.timeout_at == at("10:13", 58)


def r_timed(r, now):
    from engine.exit_manager import runner_timed_out

    return runner_timed_out(r, now)


def test_tp4_on_the_timeout_tick_sells_first():
    from brokers.base import Exchange, InstrumentKey, InstrumentType

    r, parts = _armed(timeout_min=20)
    st = ExitState(trade_db_id=1, trade_ref="T", instance_id=None, instance_name="x", account_id=1, mode="paper",
                   direction="CE_BUY", spot_series="s", option_series="o",
                   option_key=InstrumentKey(underlying="NIFTY", exchange=Exchange.NFO, instrument_type=InstrumentType.CE),
                   spot_key=None, entry_price=176.35, quantity=130, exit_at=at("15:15"), spot_sl=23600.0,
                   partials=[{**p, "basis": "premium", "side": "above", "done": bool(p["filled"])} for p in parts],
                   runner=r)
    late = r.timeout_at
    assert evaluate_exit(st, 23650.0, 220.45, late) is None  # TP4 reached: the partial sells first
    assert evaluate_exit(st, 23650.0, 215.0, late) == "runner_timeout"
    assert evaluate_exit(st, 23599.0, 215.0, late) == "spot_stop_loss"  # spot SL still first


async def test_timeout_live_exits_the_rest(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp",
                                                "params": {**TRADE2, "runner_timeout_min": 3}}])
    try:
        lt = await _enter(env, 34.70)
        await _tick(env, lt, 41.65, 390)  # TP1 fills at 09:52:31
        await _tick(env, lt, 45.15, 130)
        await _tick(env, lt, 50.0)
        assert lt.runner.timeout_at == at("09:55", 31) and lt.id in env.platform.trades.live
        assert "timeout 09:55:31" in env.platform.trades.live_view()[0]["runner"]
        await env.at("09:55", 31)
        await env.wait_for(lambda: not env.platform.trades.live)
        t = _row(env, lt)
        assert t.exit_reason == "runner_timeout"
        assert "timeout=09:55:31" in env.messages("EXIT")[-1]
    finally:
        await env.platform.stop()


def test_new_settings_checks():
    s = NiftyTwoEntryTPSettings(runner_protect=True, tp_mode="four_by_premium", runner_step_floor="tp1",
                                runner_buffer_pct=0.5, runner_timeout_min=20)
    assert s.runner_plan()["step"] == "tp1" and s.runner_plan()["buffer_pct"] == 0.5
    assert s.runner_plan()["timeout_min"] == 20
    assert (NiftyTwoEntryTPSettings().runner_buffer_pct, NiftyTwoEntryTPSettings().runner_timeout_min) == (0.0, 0)
    with pytest.raises(ValidationError, match="price just sold"):
        NiftyTwoEntryTPSettings(runner_protect=True, runner_start="tp1", runner_step_floor="tp1")
    for kw in ({"runner_buffer_pct": -1}, {"runner_buffer_pct": 25}, {"runner_timeout_min": 400}):
        with pytest.raises(ValidationError):
            NiftyTwoEntryTPSettings(runner_protect=True, **kw)
