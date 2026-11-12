"""Sideways filter in the slot (reference-candle) strategies, the engine's shared candle buffer and the
one-request-per-candle rule for broker history."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from brokers.base import Exchange, InstrumentKey, InstrumentType
from core.clock import FakeClock
from strategies.nifty_early_breakout import NiftyEarlyBreakout, NiftyEarlyBreakoutSettings
from strategies.nifty_ref_breakout import SlotState
from strategies.nifty_two_entry import NiftyTwoEntry, NiftyTwoEntrySettings
from strategies.regime_filter import REGIME_FIELDS
from tests.conftest import DAY, FakeStrategyContext, at, make_candle
from tests.mock_broker import MockBroker
from tests.test_early_breakout import open_window, spot_tick
from tests.test_engine_units import _md

KEY = InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)


def _trend_history(ctx: FakeStrategyContext, n: int = 120, step: float = 6.0) -> None:
    """`n` earlier 5-minute candles rising `step` points each (a clean trend: not sideways)."""
    t0, px = at("09:15") - timedelta(days=1), 23000.0
    for i in range(n):
        o, px = px, px + step + (3 if i % 2 else 0) - 1.5
        ctx.history_candles.append(make_candle(t0 + timedelta(minutes=5 * i), o, max(o, px) + 1, min(o, px) - 1, px))


async def _breakout(**kw):
    ctx = FakeStrategyContext(start=at("09:16"))
    _trend_history(ctx)
    strat = NiftyEarlyBreakout(NiftyEarlyBreakoutSettings(**kw), ctx)
    await strat.on_start()
    slot = await open_window(ctx, strat, "09:30", 23100, 23080)
    ctx.clock.set(at("09:36"))
    await strat.on_tick(spot_tick(23110))
    return ctx, strat, slot


# ---------------------------------------------------------------- slot strategies
async def test_slot_log_only_records_and_trades():
    ctx, strat, slot = await _breakout()
    assert ctx.signals and slot.state is SlotState.IN_TRADE
    rg = ctx.signals[0].meta["regime_entry"]
    assert rg["mode"] == "log_only" and not rg["would_block"] and rg["metrics"]["adx"] is not None
    assert strat.status()["slots"][0]["regime"] == rg["note"]
    assert any(e["type"] == "REGIME" for e in ctx.events)
    assert ctx.day_state[DAY]["slots"]["09:30"]["regime"] == rg  # kept across a restart


async def test_slot_block_skips_when_sideways():
    ctx, strat, slot = await _breakout(regime_filter="block", regime_max_chop=1, regime_min_range_atr=50)
    assert not ctx.signals
    assert (slot.state, slot.result) == (SlotState.SKIPPED, "sideways")
    assert any(e["type"] == "REGIME_BLOCKED" for e in ctx.events)
    await strat.on_tick(spot_tick(23150))  # first breakout only: the slot never trades later
    assert not ctx.signals


async def test_slot_log_only_never_blocks_even_when_sideways():
    ctx, strat, slot = await _breakout(regime_max_chop=1, regime_min_range_atr=50)
    assert ctx.signals and ctx.signals[0].meta["regime_entry"]["would_block"]


async def test_slot_off_computes_nothing():
    ctx, strat, slot = await _breakout(regime_filter="off", regime_max_chop=1, regime_min_range_atr=50)
    assert ctx.signals and ctx.signals[0].meta["regime_entry"] is None


def test_every_slot_template_has_the_filter_last_in_the_form():
    from app.web.forms import build_fields

    for model in (NiftyEarlyBreakoutSettings, NiftyTwoEntrySettings):
        names = [f.name for f in build_fields(model)]
        assert names[-len(REGIME_FIELDS):] == list(REGIME_FIELDS)
        assert model().regime_filter == "log_only"
    assert NiftyTwoEntry.warmup_days == 4  # backtests load earlier days for the filter


# ---------------------------------------------------------------- engine: shared buffer, one request per candle
class _CountingBroker(MockBroker):
    calls = 0

    async def get_historical_candles(self, instrument, timeframe_min, start, end):
        type(self).calls += 1
        await asyncio.sleep(0.01)  # an HTTP round trip
        return await super().get_historical_candles(instrument, timeframe_min, start, end)


async def _service(now: str = "09:51"):
    MockBroker.expiries = [DAY]
    _CountingBroker.calls = 0
    clock = FakeClock(at(now))
    feed = _CountingBroker(clock=clock)
    await feed.login()
    md = _md(feed, clock)
    inst = await md.subscribe(KEY, lambda s, t: None)
    t0 = at("09:15") - timedelta(days=1)
    for i in range(80):
        t = t0 + timedelta(minutes=5 * i)
        feed.history[t] = make_candle(t, 23000 + i, 23002 + i, 22999 + i, 23001 + i)
    feed.history[at("09:45")] = make_candle(at("09:45"), 100, 112, 95, 105)
    return md, inst, feed, clock


async def test_same_candle_asked_by_many_strategies_is_one_broker_request():
    md, inst, feed, clock = await _service()
    got = await asyncio.gather(*(md.get_candle(inst, at("09:45"), 5) for _ in range(12)))
    assert _CountingBroker.calls == 1 and all(c.high == 112 for c in got)
    await md.get_candle(inst, at("09:45"), 5)  # cached afterwards
    assert _CountingBroker.calls == 1


async def test_recent_candles_load_once_and_follow_closing_candles():
    md, inst, feed, clock = await _service()
    a, b = await asyncio.gather(md.recent_candles(inst, 5), md.recent_candles(inst, 5))
    assert _CountingBroker.calls == 1 and len(a) == len(b) == 81
    new = make_candle(at("09:50"), 200, 201, 199, 200)
    md._on_candle_closed("NSE:NIFTY:INDEX:-:-", new)
    md._on_candle_closed("NSE:NIFTY:INDEX:-:-", new)  # the same candle twice is kept once
    c = await md.recent_candles(inst, 5)
    assert _CountingBroker.calls == 1 and c[-1] == new and len(c) == 82
    assert len(await md.recent_candles(inst, 5, 10)) == 10


async def test_recent_candles_retry_after_failure_and_refill_a_feed_gap():
    md, inst, feed, clock = await _service()
    md.feed = None
    assert await md.recent_candles(inst, 5) == []  # no feed yet: nothing, and try again later
    await md.ensure_feed()
    assert len(await md.recent_candles(inst, 5)) == 81
    calls = _CountingBroker.calls
    clock.set(at("11:00"))  # ticks stopped for an hour: the buffer has a hole, refill it from history
    await md.recent_candles(inst, 5)
    assert _CountingBroker.calls == calls + 1
    await md.recent_candles(inst, 5)  # at most once per 10 minutes
    assert _CountingBroker.calls == calls + 1
