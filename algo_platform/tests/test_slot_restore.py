"""Today's slots survive a restart: reference candle, triggers and result are kept, and a slot that
already signalled (or was rejected) is never traded again."""
from __future__ import annotations

from brokers.base import Exchange, Tick
from strategies.nifty_ref_breakout import NiftyRefBreakout, NiftyRefBreakoutSettings, SlotState
from tests.conftest import FakeStrategyContext, at, make_candle

REF = ("09:45", 23639.20, 23609.20)


def tick(p: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=p)


async def start(ctx: FakeStrategyContext) -> NiftyRefBreakout:
    s = NiftyRefBreakout(NiftyRefBreakoutSettings(), ctx)
    await s.on_start()
    s.restore([])
    return s


async def open_first_window(ctx: FakeStrategyContext, strat: NiftyRefBreakout):
    start_hm, hi, lo = REF
    ctx.candles[at(start_hm)] = make_candle(at(start_hm), (hi + lo) / 2, hi, lo, (hi + lo) / 2)
    ctx.clock.set(at("09:50", 5))
    await strat.on_timer(ctx.now())
    return strat.slots[0]


async def restart(ctx: FakeStrategyContext, hhmm: str, sec: int = 0) -> NiftyRefBreakout:
    """A new strategy object on the same context = the app restarted (memory gone, database kept)."""
    ctx.clock.set(at(hhmm, sec))
    ctx.candles.clear()  # nothing to re-read: values must come from the saved state
    strat = await start(ctx)
    await strat.on_timer(ctx.now())
    return strat


async def test_finished_slot_keeps_reference_after_restart():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await start(ctx)
    await open_first_window(ctx, strat)
    ctx.clock.set(at("09:59", 58))
    await strat.on_timer(ctx.now())
    assert strat.slots[0].result == "no_breakout"
    s = (await restart(ctx, "10:03")).slots[0]
    assert s.state is SlotState.SKIPPED and s.result == "no_breakout"
    assert (s.ref.high, s.ref.low) == (23639.20, 23609.20)
    assert (s.long_trigger, s.short_trigger) == (23641.45, 23606.95)
    d = s.to_dict()
    assert d["ref_high"] == 23639.20 and d["long_trigger"] == 23641.45


async def test_traded_slot_keeps_reference_after_restart():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await start(ctx)
    await open_first_window(ctx, strat)
    ctx.clock.set(at("09:52"))
    await strat.on_tick(tick(23660.0))
    assert strat.slots[0].state is SlotState.IN_TRADE
    ctx.clock.set(at("10:03"))
    strat2 = NiftyRefBreakout(NiftyRefBreakoutSettings(), ctx)
    await strat2.on_start()
    from strategies.base import TradeInfo

    strat2.restore([TradeInfo(trade_id="T1", slot_id=strat.slots[0].slot_id, direction="CE_BUY", status="CLOSED",
                              exit_reason="timed_exit")])
    s = strat2.slots[0]
    assert s.state is SlotState.DONE and s.result == "timed_exit" and s.trade_id == "T1"
    assert s.ref.high == 23639.20 and s.long_trigger == 23641.45


async def test_rejected_signal_is_not_retaken_after_restart_mid_window():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await start(ctx)
    await open_first_window(ctx, strat)
    ctx.reject_next = True
    ctx.clock.set(at("09:52"))
    await strat.on_tick(tick(23660.0))
    assert strat.slots[0].result == "signal_rejected" and len(ctx.signals) == 1
    strat2 = await restart(ctx, "09:53")  # still inside the entry window
    assert strat2.slots[0].state is SlotState.SKIPPED
    await strat2.on_tick(tick(23670.0))
    assert len(ctx.signals) == 1  # first breakout only — no second attempt after the restart


async def test_crash_during_entry_is_not_retaken():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await start(ctx)
    await open_first_window(ctx, strat)

    async def crash(signal):  # the app dies after deciding to enter, before the order result
        ctx.signals.append(signal)
        raise SystemExit

    ctx.submit_entry = crash  # type: ignore[method-assign]
    ctx.clock.set(at("09:52"))
    try:
        await strat.on_tick(tick(23660.0))
    except SystemExit:
        pass
    del ctx.submit_entry
    strat2 = await restart(ctx, "09:53")
    s = strat2.slots[0]
    assert s.state is SlotState.SKIPPED and s.result == "interrupted_entry"
    await strat2.on_tick(tick(23670.0))
    assert len(ctx.signals) == 1


async def test_watching_slot_resumes_after_restart():
    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await start(ctx)
    await open_first_window(ctx, strat)
    strat2 = await restart(ctx, "09:53")  # no breakout yet: the window carries on
    assert strat2.slots[0].state is SlotState.WATCHING and strat2.slots[0].long_trigger == 23641.45
    await strat2.on_tick(tick(23660.0))
    assert len(ctx.signals) == 1 and ctx.signals[0].exit_plan.exit_at == at("09:59", 58)


async def test_slot_finished_while_app_was_off_shows_reference_but_never_trades():
    ctx = FakeStrategyContext(start=at("10:03"))
    start_hm, hi, lo = REF
    ctx.candles[at(start_hm)] = make_candle(at(start_hm), (hi + lo) / 2, hi, lo, (hi + lo) / 2)
    strat = await start(ctx)
    await strat.on_timer(ctx.now())  # fetches one missed reference candle per timer tick
    s = strat.slots[0]
    assert s.state is SlotState.MISSED and s.result == "app_was_off"
    assert s.ref.high == hi and s.long_trigger == 23641.45
    await strat.on_tick(tick(23700.0))
    assert ctx.signals == []
    # and it is remembered for the next restart
    s2 = (await restart(ctx, "10:04")).slots[0]
    assert s2.state is SlotState.MISSED and s2.ref.high == hi


async def test_restart_on_holiday_shows_last_trading_day():
    """24-Nov: the app restarted on a market holiday and the slots page showed every slot of the (holiday) day as
    MISSED / started_after_slot. On a non-trading day the last trading day's saved slots are shown instead."""
    from datetime import timedelta

    ctx = FakeStrategyContext(start=at("09:40"))
    strat = await start(ctx)
    await open_first_window(ctx, strat)
    ctx.clock.set(at("09:59", 58))
    await strat.on_timer(ctx.now())
    day = ctx.now().date()
    ctx.holidays = {day + timedelta(days=1)}
    ctx.clock.set(at("20:50") + timedelta(days=1))  # the next day is a holiday
    ctx.candles.clear()
    s = await start(ctx)
    assert s.day == day  # not the holiday
    assert s.slots[0].state is SlotState.SKIPPED and s.slots[0].result == "no_breakout"
    assert (s.slots[0].ref.high, s.slots[0].ref.low) == (23639.20, 23609.20)
    assert all(x.result != "started_after_slot" for x in s.slots if x.ref is not None)
    assert s.status()["day"] == day.isoformat()


async def test_restart_with_a_slot_saved_without_reference_candle():
    """25 Nov: the session started late, slot #1 was skipped (no_reference_candle) and saved without a candle;
    the evening restart crashed rebuilding its triggers."""
    ctx = FakeStrategyContext(start=at("09:40"))
    ctx.day_state[at("09:40").date()] = {"slots": {REF[0]: {"state": "SKIPPED", "result": "no_reference_candle",
                                                             "ref": None, "long_trigger": None, "short_trigger": None}}}
    s = (await restart(ctx, "20:54")).slots[0]
    assert s.state is SlotState.SKIPPED and s.result == "no_reference_candle" and s.ref is None
    assert s.long_trigger is None
