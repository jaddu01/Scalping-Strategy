"""NIFTY 2-Entry Breakout ("2 entry strategy_nifty.xlsx") and its TP1/TP2 version."""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backtest.engine import BacktestConfig, run_backtest
from brokers.base import Candle, Exchange, Tick
from core.clock import combine
from storage.models import Trade
from strategies.base import Direction, TradeInfo
from strategies.nifty_ref_breakout import SlotState, build_slots
from strategies.nifty_two_entry import NiftyTwoEntry, NiftyTwoEntrySettings, NiftyTwoEntryTP, NiftyTwoEntryTPSettings
from strategies.registry import all_templates
from tests.conftest import DAY, FakeStrategyContext, at, make_candle
from tests.engine_harness import build_env


def spot_tick(p: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=p)


async def started(ctx, cls=NiftyTwoEntry, settings=None):
    strat = cls(settings or NiftyTwoEntrySettings(), ctx)
    await strat.on_start()
    return strat


async def open_window(ctx, strat, ref_start: str, high: float, low: float):
    ctx.candles[at(ref_start)] = make_candle(at(ref_start), (high + low) / 2, high, low, (high + low) / 2)
    slot = next(s for s in strat.slots if s.ref_start == at(ref_start))
    ctx.clock.set(slot.ref_end.replace(second=5))
    await strat.on_timer(ctx.now())
    assert slot.state is SlotState.WATCHING
    return slot


# ---------------------------------------------------------------- schedule + defaults
def test_registered_and_defaults_match_sheet():
    t = all_templates()
    assert t["nifty_two_entry"] is NiftyTwoEntry and t["nifty_two_entry_tp"] is NiftyTwoEntryTP
    s = NiftyTwoEntrySettings()
    assert (s.lots, s.buffer_points, s.candle_timeframe_min, s.exit_early_sec) == (10, 2.25, 5, 0)
    got = [(x.ref_start.strftime("%H:%M"), x.ref_end.strftime("%H:%M"), x.entry_deadline.strftime("%H:%M:%S"),
            x.exit_at.strftime("%H:%M:%S")) for x in build_slots(s, DAY)]
    # entry from the reference close until the sharp hard exit
    assert got == [("09:45", "09:50", "13:30:00", "13:30:00"), ("13:30", "13:35", "15:15:00", "15:15:00")]


def test_slots_text_is_normalised():
    s = NiftyTwoEntrySettings(slots=" 13:30 - 15:15 ;09:45-13:30 ")
    assert s.slots == "09:45-13:30, 13:30-15:15"
    assert NiftyTwoEntrySettings(slots=["09:45-13:30"]).slots == "09:45-13:30"
    early = NiftyTwoEntrySettings(exit_early_sec=2)
    assert [x.exit_at.strftime("%H:%M:%S") for x in build_slots(early, DAY)] == ["13:29:58", "15:14:58"]


@pytest.mark.parametrize("slots, msg", [
    ("09:45", "HH:MM-HH:MM"),
    ("09:45-1330", "times must be HH:MM"),
    ("", "at least one slot"),
    ("09:47-13:30", "not aligned"),
    ("09:00-10:00", "outside market hours"),
    ("09:45-09:50", "after the reference candle closes"),
    ("13:30-15:45", "after market close"),
    ("09:45-13:30, 13:00-15:15", "must not overlap"),
    ("09:45-13:30, 09:45-15:15", "unique"),
])
def test_validation(slots, msg):
    with pytest.raises(ValidationError, match=msg):
        NiftyTwoEntrySettings(slots=slots)


def test_shared_validation_still_applies():
    with pytest.raises(ValidationError, match="BSE"):
        NiftyTwoEntrySettings(underlying="SENSEX")
    with pytest.raises(ValidationError, match="take_profit_value"):
        NiftyTwoEntrySettings(take_profit_mode="spot_points")
    with pytest.raises(ValidationError):
        NiftyTwoEntrySettings(lots=0)
    with pytest.raises(ValidationError, match="at least one candle must close"):
        NiftyTwoEntrySettings(slots="09:45-09:55", entry_trigger="candle_close")  # no candle closes before 09:55
    # 09:50-09:55 closes before a 10:00 exit, so this one can trigger
    assert NiftyTwoEntrySettings(slots="09:45-10:00", entry_trigger="candle_close").slots == "09:45-10:00"
    assert NiftyTwoEntrySettings(slots="9:45-13:30").slots == "09:45-13:30"


# ---------------------------------------------------------------- rules
async def test_late_breakout_in_long_window_enters():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await started(ctx)
    slot = await open_window(ctx, strat, "09:45", 23639.20, 23609.20)
    for hhmm, px in (("10:30", 23630.0), ("12:00", 23615.0)):  # quiet for hours
        ctx.clock.set(at(hhmm))
        await strat.on_timer(ctx.now())
        await strat.on_tick(spot_tick(px))
    assert not ctx.signals and slot.state is SlotState.WATCHING
    ctx.clock.set(at("12:40"))
    await strat.on_tick(spot_tick(23642.0))
    sig = ctx.signals[0]
    assert sig.direction is Direction.CE_BUY and sig.lots == 10 and sig.option.strike == 23650
    assert sig.exit_plan.spot_sl == 23609.20 and sig.exit_plan.exit_at == at("13:30")


async def test_pe_and_no_entry_at_or_after_exit():
    ctx = FakeStrategyContext(start=at("13:25"))
    strat = await started(ctx)
    slot = await open_window(ctx, strat, "13:30", 23639.20, 23609.20)
    ctx.clock.set(at("15:15"))
    await strat.on_timer(ctx.now())
    await strat.on_tick(spot_tick(23500.0))
    assert not ctx.signals and slot.result == "no_breakout"
    ctx2 = FakeStrategyContext(start=at("13:25"))
    strat2 = await started(ctx2)
    await open_window(ctx2, strat2, "13:30", 23639.20, 23609.20)
    ctx2.clock.set(at("15:14", 59))
    await strat2.on_tick(spot_tick(23606.0))
    sig = ctx2.signals[0]
    assert sig.direction is Direction.PE_BUY and sig.exit_plan.spot_sl == 23639.20
    assert sig.exit_plan.exit_at == at("15:15")


async def test_second_slot_trades_after_first_closed_and_no_reentry():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await started(ctx)
    s1 = await open_window(ctx, strat, "09:45", 23639.20, 23609.20)
    ctx.clock.set(at("10:10"))
    await strat.on_tick(spot_tick(23645.0))
    assert len(ctx.signals) == 1 and s1.state is SlotState.IN_TRADE
    # spot SL hit at 10:40: the slot is done, no re-entry even though the window runs to 13:30
    ctx.open_position = False
    await strat.on_trade_update(TradeInfo(trade_id="T1", slot_id=s1.slot_id, direction="CE_BUY", status="CLOSED",
                                          exit_reason="spot_stop_loss"))
    ctx.clock.set(at("11:00"))
    await strat.on_tick(spot_tick(23660.0))
    assert len(ctx.signals) == 1 and s1.result == "spot_stop_loss"
    await open_window(ctx, strat, "13:30", 23700.0, 23680.0)
    ctx.clock.set(at("14:00"))
    await strat.on_tick(spot_tick(23677.0))
    assert len(ctx.signals) == 2 and ctx.signals[1].direction is Direction.PE_BUY
    assert ctx.signals[1].exit_plan.exit_at == at("15:15")


# ---------------------------------------------------------------- TP version
def test_tp_defaults_and_validation():
    s = NiftyTwoEntryTPSettings()
    assert (s.tp_basis, s.tp1_value, s.tp2_value, s.split(), s.lots, s.exit_early_sec) == \
           ("premium_pct", 30.0, 40.0, (4, 4), 10, 0)
    assert s.slots == "09:45-13:30, 13:30-15:15"
    assert NiftyTwoEntryTPSettings(tp1_value=25, tp2_value=35).tp2_value == 35  # configurable
    with pytest.raises(ValidationError, match="greater than TP1"):
        NiftyTwoEntryTPSettings(tp1_value=40, tp2_value=30)
    with pytest.raises(ValidationError, match="less than Lots"):
        NiftyTwoEntryTPSettings(tp1_lots=5, tp2_lots=5)
    with pytest.raises(ValidationError, match="must not overlap"):
        NiftyTwoEntryTPSettings(slots="09:45-13:30, 13:00-15:15")


async def test_tp_signal_targets():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await started(ctx, NiftyTwoEntryTP, NiftyTwoEntryTPSettings())
    await open_window(ctx, strat, "09:45", 23639.20, 23609.20)
    ctx.clock.set(at("10:10"))
    await strat.on_tick(spot_tick(23645.0))
    sig = ctx.signals[0]
    assert sig.lots == 10 and sig.exit_plan.exit_at == at("13:30")
    assert [(p.label, p.premium_pct, p.lots) for p in sig.exit_plan.partial_targets] == [("tp1", 30.0, 4),
                                                                                          ("tp2", 40.0, 4)]


# ---------------------------------------------------------------- engine + backtest
async def test_engine_holds_to_the_1330_hard_exit(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "2E", "template": "nifty_two_entry"}])
    try:
        env.ref_candle("09:45", 23639.20, 23609.20)
        env.spot(23630.0)
        env.option_price(23650, "CE", 100.0)
        await env.at("09:50", 6)
        await env.at("11:20")
        env.spot(23660.0)
        await env.wait_for(lambda: env.platform.trades.live)
        lt = next(iter(env.platform.trades.live.values()))
        assert lt.lots == 10 and lt.entry_qty == 650 and lt.exit_at == at("13:30")
        env.option_price(23650, "CE", 90.0)
        await env.at("13:29", 59)
        assert env.platform.trades.live
        await env.at("13:30", 0)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.scalar(select(Trade))
        assert t.exit_reason == "timed_exit" and t.exit_time.strftime("%H:%M:%S") == "13:30:00"
        assert t.gross_pnl == pytest.approx(-10 * 650)
    finally:
        await env.platform.stop()


async def test_backtest_runs_the_two_slots():
    day = date(2026, 11, 4)
    bars = {}
    t = combine(day, time(9, 15))
    while t < combine(day, time(15, 30)):
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23601, low=23599, close=23600)
        t += timedelta(minutes=1)
    for i in range(5):  # slot 1 reference
        t = combine(day, time(9, 45 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    t = combine(day, time(11, 40))  # breakout two hours later
    bars[t] = Candle(start=t, timeframe_min=1, open=23605, high=23615, low=23604, close=23614)
    t = combine(day, time(11, 41))
    while t < combine(day, time(13, 30)):
        bars[t] = Candle(start=t, timeframe_min=1, open=23614, high=23615, low=23613, close=23614)
        t += timedelta(minutes=1)
    res = await run_backtest(BacktestConfig(template_id="nifty_two_entry", params={}, day_from=day, day_to=day),
                             list(bars.values()))
    trades = res["trades"]
    assert len(trades) == 1
    tr = trades[0]
    assert tr["direction"] == "CE_BUY" and tr["signal_time"].startswith("11:40")
    assert tr["exit_reason"] == "timed_exit" and tr["exit_time"] == "13:30:00" and tr["qty"] == 650
