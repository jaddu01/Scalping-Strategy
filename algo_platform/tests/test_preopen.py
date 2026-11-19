"""NSE pre-open (09:00-09:15) and after-close prices never become candles and never reach the strategies.

19-Nov-2026: 09:00 / 09:05 / 09:10 candles built from pre-open index ticks flipped the Chandelier lines (BUY 09:00,
SELL 09:05) and armed an EMA21/RSI50 setup on a 98-point pre-open candle; the broker's candles and the
recordings start at 09:15."""
from __future__ import annotations

from datetime import timedelta

from brokers.base import Exchange, InstrumentKey, InstrumentType
from core.clock import FakeClock
from strategies.base import Strategy, StrategySettings
from strategies.registry import register
from tests.conftest import DAY, at
from tests.engine_harness import build_env
from tests.mock_broker import SPOT_TOKEN, MockBroker
from tests.test_engine_units import _md

SPOT = "NSE:NIFTY:INDEX:-:-"


async def test_preopen_ticks_make_no_candles_but_update_ltp():
    MockBroker.expiries = [DAY]
    clock = FakeClock(at("09:00"))
    feed = MockBroker(clock=clock)
    await feed.login()
    md = _md(feed, clock)
    closed = []
    md.builder.add_listener(lambda s, c: closed.append(c))
    key = InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)
    await md.subscribe(key, lambda s, t: None)
    md.builder.track(SPOT, 5)
    for m, px in ((0, 25244.1), (4, 25270.6), (7, 25172.3), (8, 25186.8), (14, 25190.0)):  # pre-open
        clock.set(at("09:00") + timedelta(minutes=m, seconds=30))
        feed.push(SPOT_TOKEN, px)
        md.builder.on_time(clock.now())
    assert closed == [] and md.builder.working(SPOT, 5) is None
    assert md.ltp(SPOT) == 25190.0  # the pre-open price is still shown
    for sec, px in ((0, 25186.8), (90, 25210.5), (200, 25151.5), (290, 25191.8)):  # regular session
        clock.set(at("09:15") + timedelta(seconds=sec))
        feed.push(SPOT_TOKEN, px)
    clock.set(at("09:20", 2))
    md.builder.on_time(clock.now())
    assert [(c.start, c.open, c.high, c.low, c.close, c.complete) for c in closed] == [
        (at("09:15"), 25186.8, 25210.5, 25151.5, 25191.8, True)]  # complete: the feed was running before 09:15


async def test_after_close_ticks_make_no_candles():
    MockBroker.expiries = [DAY]
    clock = FakeClock(at("15:25"))
    feed = MockBroker(clock=clock)
    await feed.login()
    md = _md(feed, clock)
    closed = []
    md.builder.add_listener(lambda s, c: closed.append(c))
    await md.subscribe(InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX),
                       lambda s, t: None)
    md.builder.track(SPOT, 5)
    for t, px in ((at("15:25", 10), 25300.0), (at("15:29", 50), 25310.0), (at("15:31"), 25400.0), (at("15:40"), 25500.0)):
        clock.set(t)
        feed.push(SPOT_TOKEN, px)
        md.builder.on_time(t)
    assert [(c.start, c.close) for c in closed] == [(at("15:25"), 25310.0)]  # 15:31 / 15:40 prices: no candle


SEEN: list[tuple[str, object]] = []


@register
class _Recorder(Strategy):
    template_id = "test_preopen_recorder"
    display_name = "Records what it receives"
    settings_model = StrategySettings

    def subscriptions(self):
        return [InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)]

    def candle_timeframes(self):
        return [5]

    async def on_tick(self, tick):
        SEEN.append(("tick", self.ctx.now()))

    async def on_candle(self, instrument, candle):
        SEEN.append(("candle", candle.start))

    async def on_timer(self, now):
        SEEN.append(("timer", now))


async def test_strategy_gets_no_preopen_ticks_or_candles_but_its_timer_runs(tmp_path):
    SEEN.clear()
    env = await build_env(tmp_path, start=at("08:59"), instances=[{"name": "REC", "template": "test_preopen_recorder"}])
    try:
        for m in range(0, 15, 2):  # pre-open ticks 09:00 .. 09:14
            env.clock.set(at("09:00") + timedelta(minutes=m, seconds=20))
            env.spot(25200.0 + m)
            await env.settle(2)
        pre = list(SEEN)
        assert pre and all(kind == "timer" for kind, _ in pre)  # timers run, nothing else
        for sec in (5, 100, 200, 290):
            env.clock.set(at("09:15") + timedelta(seconds=sec))
            env.spot(25220.0 + sec / 100)
            await env.settle(2)
        env.clock.set(at("09:20", 1))
        env.spot(25223.0)  # the next candle's first tick closes 09:15
        await env.settle(4)
        kinds = {k for k, _ in SEEN[len(pre):]}
        assert {"tick", "timer"} <= kinds
        candles = [v for k, v in SEEN if k == "candle"]
        assert candles == [at("09:15")]  # the first candle of the day is 09:15
    finally:
        await env.platform.stop()
