"""Rules of the NIFTY reference candle breakout strategy, including the five known bugs."""
from __future__ import annotations

from datetime import time

import pytest
from pydantic import ValidationError

from brokers.base import InstrumentType, Tick, Exchange
from strategies.base import Direction, TakeProfitMode, TradeInfo
from strategies.nifty_ref_breakout import (
    NiftyRefBreakout,
    NiftyRefBreakoutSettings,
    SlotState,
    atm_strike,
    breakout_direction,
    build_slots,
    pick_expiry,
    spot_sl_hit,
    spot_stop,
    take_profit_level,
    triggers,
)
from tests.conftest import DAY, NEXT_TUE, FakeStrategyContext, at, make_candle

EXPECTED_SCHEDULE = [
    ("09:45", "09:50", "10:00"), ("10:00", "10:05", "10:15"), ("10:15", "10:20", "10:30"),
    ("10:45", "10:50", "11:00"), ("11:15", "11:20", "11:30"), ("11:45", "11:50", "12:00"),
    ("12:10", "12:15", "12:25"), ("12:45", "12:50", "13:00"), ("13:15", "13:20", "13:30"),
    ("13:45", "13:50", "14:00"), ("14:10", "14:15", "14:25"), ("14:35", "14:40", "14:50"),
    ("14:55", "15:00", "15:10"),
]


def spot_tick(price: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=price)


async def started(ctx: FakeStrategyContext, **overrides) -> NiftyRefBreakout:
    strat = NiftyRefBreakout(NiftyRefBreakoutSettings(**overrides), ctx)
    await strat.on_start()
    return strat


async def open_window(ctx: FakeStrategyContext, strat: NiftyRefBreakout, ref_start: str, high: float, low: float):
    ctx.candles[at(ref_start)] = make_candle(at(ref_start), (high + low) / 2, high, low, (high + low) / 2)
    slot = next(s for s in strat.slots if s.ref_start == at(ref_start))
    ctx.clock.set(slot.ref_end.replace(second=5))
    await strat.on_timer(ctx.now())
    assert slot.state is SlotState.WATCHING
    return slot


# ---------------------------------------------------------------- defaults (bug 5: 5 lots)
def test_defaults_match_spec():
    s = NiftyRefBreakoutSettings()
    assert s.lots == 5
    assert s.buffer_points == 2.25
    assert s.exit_offset_min == 15
    assert s.entry_window_candles == 2
    assert s.candle_timeframe_min == 5
    assert s.strike_step == 50
    assert s.protective_sl_pct == 0  # optional broker disaster stop; not in the strategy rules
    assert s.take_profit_mode is TakeProfitMode.NONE
    assert s.entry_trigger == "tick" and s.entry_order_type == "MARKET"
    assert s.candle_finalization_delay_sec == 5


# ---------------------------------------------------------------- bug 3 + bug 2: fixed schedule, exit = start + 15
def test_fixed_slot_schedule_and_exit_times():
    slots = build_slots(NiftyRefBreakoutSettings(), DAY)
    got = [(s.ref_start.strftime("%H:%M"), s.ref_end.strftime("%H:%M"), s.exit_at.strftime("%H:%M")) for s in slots]
    assert got == EXPECTED_SCHEDULE
    assert [s.no for s in slots] == list(range(1, 14))
    # entry window = reference close -> reference close + 10 minutes
    assert slots[0].entry_end.strftime("%H:%M") == "10:00"


def test_exit_is_start_plus_15_not_20():
    s = build_slots(NiftyRefBreakoutSettings(), DAY)
    by_start = {x.ref_start.strftime("%H:%M"): x.exit_at.strftime("%H:%M") for x in s}
    assert by_start["09:45"] == "10:00"
    assert by_start["12:10"] == "12:25"
    assert by_start["10:45"] == "11:00"  # the old build exited at 10:50


async def test_reference_candles_come_only_from_the_schedule(fake_ctx):
    """Old build picked 09:30, 10:30, 11:00 ... dynamically. Only scheduled starts may be requested."""
    strat = await started(fake_ctx)
    requested = []

    async def spy(inst, start, tf):
        requested.append(start.strftime("%H:%M"))
        return make_candle(start, 100, 101, 99, 100)

    fake_ctx.get_candle = spy  # type: ignore[assignment]
    for minute in range(9 * 60 + 30, 15 * 60 + 20):
        fake_ctx.clock.set(at(f"{minute // 60:02d}:{minute % 60:02d}", 30))
        await strat.on_timer(fake_ctx.now())
    assert requested == [r[0] for r in EXPECTED_SCHEDULE]


# ---------------------------------------------------------------- bug 1: buffer applied
def test_triggers_apply_buffer():
    assert triggers(23639.20, 23609.20, 2.25) == (23641.45, 23606.95)


def test_breakout_needs_strictly_more_than_buffer():
    lt, st = triggers(100.0, 90.0, 2.25)
    assert breakout_direction(102.25, lt, st) is None  # touching the trigger is not a breakout
    assert breakout_direction(102.26, lt, st) is Direction.CE_BUY
    assert breakout_direction(101.0, lt, st) is None  # above high but inside the buffer
    assert breakout_direction(87.75, lt, st) is None
    assert breakout_direction(87.74, lt, st) is Direction.PE_BUY


async def test_no_entry_inside_buffer(fake_ctx):
    strat = await started(fake_ctx)
    await open_window(fake_ctx, strat, "09:45", 23639.20, 23609.20)
    fake_ctx.clock.set(at("09:52"))
    await strat.on_tick(spot_tick(23641.0))  # above high, below high+2.25
    assert fake_ctx.signals == []
    await strat.on_tick(spot_tick(23641.50))
    assert len(fake_ctx.signals) == 1


# ---------------------------------------------------------------- bug 4: buys labelled CE_BUY / PE_BUY
async def test_signal_is_ce_buy_with_atm_strike_and_5_lots(fake_ctx):
    strat = await started(fake_ctx)
    slot = await open_window(fake_ctx, strat, "09:45", 23639.20, 23609.20)
    fake_ctx.clock.set(at("09:52", 31))
    await strat.on_tick(spot_tick(23660.10))
    sig = fake_ctx.signals[0]
    assert sig.direction is Direction.CE_BUY and sig.direction.value == "CE_BUY"
    assert sig.option.instrument_type is InstrumentType.CE
    assert sig.option.strike == 23650.0
    assert sig.option.expiry == DAY  # nearest non-expired weekly (expiry day itself counts)
    assert sig.lots == 5
    assert sig.exit_plan.spot_sl == 23609.20 and sig.exit_plan.spot_sl_side == "below"
    assert sig.exit_plan.exit_at == at("10:00")
    assert sig.trigger == 23641.45
    assert slot.state is SlotState.IN_TRADE


async def test_signal_is_pe_buy_below_low_minus_buffer(fake_ctx):
    strat = await started(fake_ctx)
    await open_window(fake_ctx, strat, "10:45", 23670.95, 23639.90)
    fake_ctx.clock.set(at("10:51"))
    await strat.on_tick(spot_tick(23637.0))
    sig = fake_ctx.signals[0]
    assert sig.direction is Direction.PE_BUY
    assert sig.option.instrument_type is InstrumentType.PE
    assert sig.exit_plan.spot_sl == 23670.95 and sig.exit_plan.spot_sl_side == "above"
    assert sig.exit_plan.exit_at == at("11:00")


def test_atm_strike_rounding():
    assert atm_strike(23624.99, 50) == 23600
    assert atm_strike(23625.00, 50) == 23650
    assert atm_strike(23649.0, 50) == 23650
    assert atm_strike(23674.9, 50) == 23650
    assert atm_strike(48210, 100) == 48200


def test_spot_stop_loss_levels():
    assert spot_stop(Direction.CE_BUY, 110, 100) == (100, "below")
    assert spot_stop(Direction.PE_BUY, 110, 100) == (110, "above")
    assert spot_sl_hit(Direction.CE_BUY, 100.0, 110, 100)
    assert not spot_sl_hit(Direction.CE_BUY, 100.05, 110, 100)
    assert spot_sl_hit(Direction.PE_BUY, 110.0, 110, 100)
    assert not spot_sl_hit(Direction.PE_BUY, 109.95, 110, 100)


def test_take_profit_levels():
    s = NiftyRefBreakoutSettings(take_profit_mode="spot_points", take_profit_value=20)
    assert take_profit_level(s, Direction.CE_BUY, 1000, 990) == (1020, "above", None)
    assert take_profit_level(s, Direction.PE_BUY, 1000, 1010) == (980, "below", None)
    s = NiftyRefBreakoutSettings(take_profit_mode="r_multiple", take_profit_value=2)
    assert take_profit_level(s, Direction.CE_BUY, 1000, 990) == (1020, "above", None)
    s = NiftyRefBreakoutSettings(take_profit_mode="premium_pct", take_profit_value=30)
    assert take_profit_level(s, Direction.CE_BUY, 1000, 990) == (None, "above", 30)


def test_pick_expiry():
    assert pick_expiry([NEXT_TUE, DAY], DAY, "nearest_weekly") == DAY
    assert pick_expiry([NEXT_TUE, DAY], DAY, "next_weekly") == NEXT_TUE
    from datetime import timedelta
    assert pick_expiry([DAY - timedelta(days=2), NEXT_TUE], DAY, "nearest_weekly") == NEXT_TUE


# ---------------------------------------------------------------- one trade per slot, no re-entry, skip
async def test_first_breakout_only_and_one_trade_per_slot(fake_ctx):
    strat = await started(fake_ctx)
    slot = await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("09:51"))
    await strat.on_tick(spot_tick(103))
    await strat.on_tick(spot_tick(104))
    await strat.on_tick(spot_tick(80))  # opposite breakout later in the same slot
    assert len(fake_ctx.signals) == 1
    # stop loss closes the trade; no re-entry in the same slot afterwards
    fake_ctx.open_position = False
    await strat.on_trade_update(TradeInfo(trade_id="T1", slot_id=slot.slot_id, direction="CE_BUY", status="CLOSED",
                                          exit_reason="spot_stop_loss"))
    assert slot.state is SlotState.DONE and slot.result == "spot_stop_loss"
    fake_ctx.clock.set(at("09:55"))
    await strat.on_tick(spot_tick(110))
    await strat.on_tick(spot_tick(70))
    assert len(fake_ctx.signals) == 1


async def test_skip_when_no_breakout_before_exit(fake_ctx):
    strat = await started(fake_ctx)
    slot = await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("09:58"))
    await strat.on_tick(spot_tick(95))
    fake_ctx.clock.set(at("10:00"))
    await strat.on_timer(fake_ctx.now())
    assert slot.state is SlotState.SKIPPED
    assert fake_ctx.messages("SLOT_SKIPPED") == ["SKIP slot=#1 reason=no_breakout"]
    await strat.on_tick(spot_tick(200))
    assert fake_ctx.signals == []


async def test_rejected_signal_does_not_retry_in_slot(fake_ctx):
    strat = await started(fake_ctx)
    slot = await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.reject_next = True
    fake_ctx.clock.set(at("09:51"))
    await strat.on_tick(spot_tick(103))
    await strat.on_tick(spot_tick(104))
    assert len(fake_ctx.signals) == 1
    assert slot.state is SlotState.SKIPPED and slot.result == "signal_rejected"


async def test_one_open_position_per_instance(fake_ctx):
    # overlapping custom schedule: second slot breaks out while the first trade is open
    strat = await started(fake_ctx, reference_candle_starts="09:45,09:50", exit_offset_min=30)
    await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("09:51"))
    await strat.on_tick(spot_tick(103))
    assert len(fake_ctx.signals) == 1
    s2 = await open_window(fake_ctx, strat, "09:50", 104.0, 101.0)
    fake_ctx.clock.set(at("09:56"))
    await strat.on_tick(spot_tick(110))
    assert len(fake_ctx.signals) == 1
    assert s2.state is SlotState.SKIPPED and s2.result == "already_in_position"


async def test_ref_log_format(fake_ctx):
    strat = await started(fake_ctx)
    await open_window(fake_ctx, strat, "09:45", 23639.20, 23609.20)
    msg = fake_ctx.messages("REF_CANDLE")[0]
    assert msg.startswith("REF slot=#1 candle=09:45 high=23639.20 low=23609.20 "
                          "long_trigger=23641.45 short_trigger=23606.95 exit=10:00")


async def test_window_opens_only_after_finalization_delay(fake_ctx):
    strat = await started(fake_ctx)
    fake_ctx.candles[at("09:45")] = make_candle(at("09:45"), 95, 100, 90, 95)
    fake_ctx.clock.set(at("09:50", 3))
    await strat.on_timer(fake_ctx.now())
    assert strat.slots[0].state is SlotState.PENDING
    fake_ctx.clock.set(at("09:50", 5))
    await strat.on_timer(fake_ctx.now())
    assert strat.slots[0].state is SlotState.WATCHING


async def test_candle_close_trigger(fake_ctx):
    strat = await started(fake_ctx, entry_trigger="candle_close", exit_offset_min=20)
    await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("09:51"))
    await strat.on_tick(spot_tick(120))  # ticks are ignored in candle_close mode
    assert fake_ctx.signals == []
    fake_ctx.clock.set(at("09:55", 5))
    await strat.on_candle(fake_ctx.spot, make_candle(at("09:50"), 99, 106, 98, 101))  # closes inside buffer
    assert fake_ctx.signals == []
    # the second window candle (09:55-10:00) closes before the 10:05 hard exit, so it can trigger
    fake_ctx.spot_ltp = 108.0
    fake_ctx.clock.set(at("10:00", 5))
    await strat.on_candle(fake_ctx.spot, make_candle(at("09:55"), 101, 110, 100, 108))
    assert fake_ctx.signals[0].direction is Direction.CE_BUY
    assert fake_ctx.signals[0].exit_plan.exit_at == at("10:05")


async def test_candle_close_ignores_candles_after_the_window(fake_ctx):
    strat = await started(fake_ctx, entry_trigger="candle_close", exit_offset_min=25)
    await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("10:05", 5))  # third candle after the reference: outside a 2-candle window
    await strat.on_candle(fake_ctx.spot, make_candle(at("10:00"), 101, 110, 100, 108))
    assert fake_ctx.signals == []


async def test_candle_close_trigger_enters_on_close_beyond_trigger(fake_ctx):
    strat = await started(fake_ctx, entry_trigger="candle_close", entry_window_candles=1)
    await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.spot_ltp = 103.0
    # the window's last candle closes at 09:55 = window end; it arrives ~2 s later and must still count
    fake_ctx.clock.set(at("09:55", 2))
    await strat.on_timer(fake_ctx.now())
    assert strat.slots[0].state is SlotState.WATCHING
    await strat.on_candle(fake_ctx.spot, make_candle(at("09:50"), 99, 106, 98, 102.5))
    assert fake_ctx.signals[0].direction is Direction.CE_BUY


async def test_candle_close_window_closes_after_grace(fake_ctx):
    strat = await started(fake_ctx, entry_trigger="candle_close", entry_window_candles=1)
    await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("09:55", 16))
    await strat.on_timer(fake_ctx.now())
    assert strat.slots[0].state is SlotState.SKIPPED and strat.slots[0].result == "no_breakout"


def test_candle_close_rejects_window_candles_that_close_at_the_exit():
    # defaults: 2-candle window, exit at start+15 -> the 2nd candle closes exactly at the exit
    with pytest.raises(ValidationError, match="could never trigger an entry"):
        NiftyRefBreakoutSettings(entry_trigger="candle_close")
    NiftyRefBreakoutSettings(entry_trigger="candle_close", entry_window_candles=1)
    NiftyRefBreakoutSettings(entry_trigger="candle_close", exit_offset_min=20)
    NiftyRefBreakoutSettings(entry_trigger="tick")  # tick mode is unaffected


async def test_started_late_marks_finished_slots_missed():
    ctx = FakeStrategyContext(start=at("11:10"))
    strat = await started(ctx)
    states = {s.ref_start.strftime("%H:%M"): s.state for s in strat.slots}
    assert states["09:45"] is SlotState.MISSED
    assert states["10:45"] is SlotState.MISSED  # exit 11:00 already passed
    assert states["11:15"] is SlotState.PENDING


async def test_settings_change_applies_only_when_idle(fake_ctx):
    strat = await started(fake_ctx)
    slot = await open_window(fake_ctx, strat, "09:45", 100.0, 90.0)
    fake_ctx.clock.set(at("09:51"))
    await strat.on_tick(spot_tick(103))
    assert slot.state is SlotState.IN_TRADE
    applied = strat.request_settings_update(NiftyRefBreakoutSettings(buffer_points=10, lots=2))
    assert not applied and strat.settings.buffer_points == 2.25
    fake_ctx.open_position = False
    await strat.on_trade_update(TradeInfo(trade_id="T1", slot_id=slot.slot_id, direction="CE_BUY",
                                          status="CLOSED", exit_reason="timed_exit"))
    assert strat.settings.buffer_points == 10 and strat.settings.lots == 2
    s2 = await open_window(fake_ctx, strat, "10:00", 100.0, 90.0)
    assert s2.long_trigger == 110.0


async def test_restore_marks_done_and_open_slots(fake_ctx):
    strat = await started(fake_ctx)
    s1, s2 = strat.slots[0], strat.slots[1]
    strat.restore([
        TradeInfo(trade_id="T1", slot_id=s1.slot_id, direction="CE_BUY", status="CLOSED", exit_reason="timed_exit"),
        TradeInfo(trade_id="T2", slot_id=s2.slot_id, direction="PE_BUY", status="OPEN"),
    ])
    assert strat.slots[0].state is SlotState.DONE
    assert strat.slots[1].state is SlotState.IN_TRADE
    assert strat.phase() == "IN_TRADE"


def test_custom_schedule_parsing_and_sorting():
    s = NiftyRefBreakoutSettings(reference_candle_starts="10:15, 09:45")
    assert s.reference_candle_starts == [time(9, 45), time(10, 15)]
    assert s.model_dump(mode="json")["reference_candle_starts"] == ["09:45", "10:15"]


@pytest.mark.parametrize("bad", [
    {"reference_candle_starts": "09:00"},          # before open
    {"reference_candle_starts": "09:47"},          # not aligned to 5 min
    {"reference_candle_starts": "09:45,09:45"},    # duplicate
    {"reference_candle_starts": "15:25"},          # exit after close
    {"buffer_points": -1},
    {"lots": 0},
    {"exit_offset_min": 5},                        # exit at candle close
    {"take_profit_mode": "spot_points"},           # needs a value
    {"entry_trigger": "bar"},
    {"unknown_field": 1},
])
def test_settings_validation_rejects_bad_values(bad):
    with pytest.raises(Exception):
        NiftyRefBreakoutSettings(**bad)


def test_settings_saved_with_retired_trailing_keys_still_load():
    s = NiftyRefBreakoutSettings.model_validate({"lots": 3, "trailing_stop": "candle", "trail_timeframe_min": 1})
    assert s.lots == 3 and "trailing_stop" not in s.model_dump()
    with pytest.raises(ValidationError):
        NiftyRefBreakoutSettings.model_validate({"not_a_setting": 1})  # other unknown keys still rejected
