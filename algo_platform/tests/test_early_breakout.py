"""Strategy part 1 (early-slot breakout), running alongside the original strategy."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from brokers.base import Exchange, InstrumentType, Tick
from storage.models import Trade
from strategies.base import Direction
from strategies.nifty_early_breakout import NiftyEarlyBreakout, NiftyEarlyBreakoutSettings
from strategies.nifty_ref_breakout import NiftyRefBreakoutSettings, SlotState, build_slots
from strategies.registry import all_templates, default_settings
from tests.conftest import DAY, FakeStrategyContext, at, make_candle
from tests.engine_harness import build_env

# (reference start, reference close, hard exit) — columns E, F, H of "strategy part 1.xlsx"
SHEET_SCHEDULE = [
    ("09:30", "09:35", "09:44:58"), ("09:45", "09:50", "09:59:58"), ("10:00", "10:05", "10:14:58"),
    ("10:30", "10:35", "10:44:58"), ("11:00", "11:05", "11:14:58"), ("11:30", "11:35", "11:44:58"),
    ("11:55", "12:00", "12:09:58"), ("12:30", "12:35", "12:44:58"), ("13:00", "13:05", "13:14:58"),
    ("13:30", "13:35", "13:44:58"), ("13:55", "14:00", "14:09:58"), ("14:20", "14:25", "14:34:58"),
    ("14:55", "15:00", "15:09:58"),
]


def spot_tick(price: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=price)


async def started(ctx: FakeStrategyContext, **overrides) -> NiftyEarlyBreakout:
    strat = NiftyEarlyBreakout(NiftyEarlyBreakoutSettings(**overrides), ctx)
    await strat.on_start()
    return strat


async def open_window(ctx, strat, ref_start: str, high: float, low: float):
    ctx.candles[at(ref_start)] = make_candle(at(ref_start), (high + low) / 2, high, low, (high + low) / 2)
    slot = next(s for s in strat.slots if s.ref_start == at(ref_start))
    ctx.clock.set(slot.ref_end.replace(second=5))
    await strat.on_timer(ctx.now())
    assert slot.state is SlotState.WATCHING
    return slot


# ---------------------------------------------------------------- template + defaults
def test_registered_as_separate_template():
    templates = all_templates()
    assert "nifty_early_breakout" in templates and "nifty_ref_breakout" in templates
    assert templates["nifty_early_breakout"] is NiftyEarlyBreakout
    d = default_settings("nifty_early_breakout")
    assert d["lots"] == 10 and d["reference_candle_starts"][0] == "09:30"
    # the original strategy keeps its own defaults
    assert default_settings("nifty_ref_breakout")["lots"] == 5
    assert default_settings("nifty_ref_breakout")["reference_candle_starts"][0] == "09:45"


def test_defaults_match_sheet():
    s = NiftyEarlyBreakoutSettings()
    assert s.lots == 10
    assert s.buffer_points == 2.25
    assert s.exit_offset_min == 15
    assert s.entry_window_candles == 2
    assert s.candle_timeframe_min == 5
    slots = build_slots(s, DAY)
    got = [(x.ref_start.strftime("%H:%M"), x.ref_end.strftime("%H:%M"), x.exit_at.strftime("%H:%M:%S")) for x in slots]
    assert got == SHEET_SCHEDULE
    # entry window = the two 5-min candles after the reference candle, closing with the timed exit
    assert slots[0].entry_deadline.strftime("%H:%M:%S") == "09:44:58"


def test_inherited_validation_still_applies():
    with pytest.raises(ValidationError):
        NiftyEarlyBreakoutSettings(reference_candle_starts="09:30, 09:30")
    with pytest.raises(ValidationError):
        NiftyEarlyBreakoutSettings(reference_candle_starts="09:32")  # not on the 5-min grid
    assert NiftyEarlyBreakoutSettings(reference_candle_starts="10:00, 09:30").reference_candle_starts[0].hour == 9


def test_shared_slots():
    early = {x.ref_start for x in build_slots(NiftyEarlyBreakoutSettings(), DAY)}
    orig = {x.ref_start for x in build_slots(NiftyRefBreakoutSettings(), DAY)}
    assert sorted(t.strftime("%H:%M") for t in early & orig) == ["09:45", "10:00", "14:55"]


def test_old_saved_setting_still_loads():
    s = NiftyEarlyBreakoutSettings.model_validate({"skip_if_other_strategy_holds": True})
    assert s.lots == 10


# ---------------------------------------------------------------- entry / SL rules
async def test_first_slot_ce_entry_10_lots():
    ctx = FakeStrategyContext(start=at("09:25"))
    strat = await started(ctx)
    slot = await open_window(ctx, strat, "09:30", 23640.0, 23610.0)
    assert slot.long_trigger == 23642.25 and slot.short_trigger == 23607.75
    ctx.clock.set(at("09:37", 10))
    await strat.on_tick(spot_tick(23642.25))  # must be strictly above high + buffer
    assert not ctx.signals
    await strat.on_tick(spot_tick(23643.0))
    sig = ctx.signals[0]
    assert sig.direction is Direction.CE_BUY and sig.lots == 10
    assert sig.option.strike == 23650 and sig.option.instrument_type is InstrumentType.CE
    assert sig.exit_plan.spot_sl == 23610.0 and sig.exit_plan.spot_sl_side == "below"
    assert sig.exit_plan.exit_at == at("09:44", 58)


async def test_pe_entry_sl_is_ref_high():
    ctx = FakeStrategyContext(start=at("14:50"))
    strat = await started(ctx)
    await open_window(ctx, strat, "14:55", 23640.0, 23610.0)
    ctx.clock.set(at("15:02"))
    await strat.on_tick(spot_tick(23607.0))
    sig = ctx.signals[0]
    assert sig.direction is Direction.PE_BUY
    assert sig.exit_plan.spot_sl == 23640.0 and sig.exit_plan.spot_sl_side == "above"
    assert sig.exit_plan.exit_at == at("15:09", 58)


async def test_no_entry_after_window():
    ctx = FakeStrategyContext(start=at("09:25"))
    strat = await started(ctx)
    slot = await open_window(ctx, strat, "09:30", 23640.0, 23610.0)
    ctx.clock.set(at("09:45"))
    await strat.on_timer(ctx.now())
    await strat.on_tick(spot_tick(23700.0))
    assert not ctx.signals and slot.result == "no_breakout"


# ---------------------------------------------------------------- both strategies in the real engine
async def test_both_strategies_trade_the_shared_slot(tmp_path):
    env = await build_env(tmp_path, instances=[
        {"name": "ORIGINAL", "template": "nifty_ref_breakout"},
        {"name": "EARLY", "template": "nifty_early_breakout"},
    ])
    try:
        orig_id, early_id = env.instance_ids
        rts = env.platform.strategies.runtimes
        await env.wait_for(lambda: orig_id in rts and early_id in rts)
        env.ref_candle("09:45", 23639.20, 23609.20)  # 09:45 is in both schedules
        env.spot(23630.0)
        env.option_price(23650, "CE", 100.0)
        await env.at("09:50", 6)
        assert len(env.messages("REF_CANDLE")) == 2
        await env.at("09:52", 31)
        env.spot(23660.0)
        await env.wait_for(lambda: len(env.platform.trades.live) == 2)
        with env.db.session() as s:
            trades = {t.instance_id: t for t in s.scalars(select(Trade))}
        assert set(trades) == {orig_id, early_id}
        assert trades[orig_id].lots == 5 and trades[early_id].lots == 10
        assert trades[orig_id].tradingsymbol == trades[early_id].tradingsymbol
        # the shared broker position (15 lots) is not mistaken for a manual exit; both exit on time
        env.option_price(23650, "CE", 110.0)
        await env.at("10:00", 0)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            reasons = {t.instance_id: t.exit_reason for t in s.scalars(select(Trade))}
        assert reasons == {orig_id: "timed_exit", early_id: "timed_exit"}
    finally:
        await env.platform.stop()
