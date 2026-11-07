"""Candle builder, reference-candle cross-check, risk checks, exit evaluation and the backtester."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backtest.engine import BacktestConfig, parse_candle_csv, run_backtest
from brokers.base import Candle, Exchange, InstrumentKey, InstrumentType, Tick
from core.clock import FakeClock
from core.events import EventBus
from core.settings_schema import GlobalSettings, InstanceRiskLimits
from engine.calendar import MarketCalendar
from engine.candles import CandleBuilder
from engine.exit_manager import ExitState, evaluate_exit
from engine.market_data import MarketDataService
from tests.conftest import DAY, at, make_candle
from tests.engine_harness import build_env
from tests.mock_broker import MockBroker


def tick(ts: datetime, px: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="26000", ltp=px, ts=ts)


def test_candle_builder_ohlc_and_completeness():
    b = CandleBuilder()
    closed = []
    b.add_listener(lambda s, c: closed.append(c))
    b.track("S", 5)
    b.on_tick("S", tick(at("09:44", 58), 100))  # subscribed before the 09:45 candle starts
    for sec, px in [(0, 101), (30, 105), (90, 99), (200, 103)]:
        b.on_tick("S", tick(at("09:45") + timedelta(seconds=sec), px))
    b.on_tick("S", tick(at("09:50", 1), 104))
    c = b.get("S", at("09:45"), 5)
    assert (c.open, c.high, c.low, c.close) == (101, 105, 99, 103) and c.complete
    # a candle that started before ticks arrived is incomplete
    b2 = CandleBuilder()
    b2.track("S", 5)
    b2.on_tick("S", tick(at("09:47"), 100))
    b2.on_time(at("09:50", 3))
    assert b2.get("S", at("09:45"), 5).complete is False


def _md(feed: MockBroker, clock: FakeClock) -> MarketDataService:
    from engine.market_data import FeedCandidate

    gs = GlobalSettings()
    md = MarketDataService(EventBus(), clock, MarketCalendar(gs), lambda: gs)
    md.candidates = lambda: [FeedCandidate(1, "Mock", feed)]
    return md


async def test_reference_candle_prefers_broker_on_mismatch():
    MockBroker.expiries = [DAY]
    clock = FakeClock(at("09:44"))
    feed = MockBroker(clock=clock)
    await feed.login()
    md = _md(feed, clock)
    events = []
    md.bus.add_sink(events.append)
    key = InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)
    inst = await md.subscribe(key, lambda s, t: None)
    md.builder.track("NSE:NIFTY:INDEX:-:-", 5)
    for sec, px in [(0, 100), (60, 110), (120, 95), (240, 105)]:
        clock.set(at("09:45") + timedelta(seconds=sec))
        feed.push("26000", px)
    clock.set(at("09:50", 1))
    feed.push("26000", 106)
    feed.history[at("09:45")] = make_candle(at("09:45"), 100, 112, 95, 105)
    clock.set(at("09:50", 6))
    c = await md.get_candle(inst, at("09:45"), 5)
    assert c.high == 112  # the broker candle (ticks saw 110)
    assert any(e.event_type == "CANDLE_MISMATCH" for e in events)


async def test_reference_candle_falls_back_to_ticks_when_history_missing():
    MockBroker.expiries = [DAY]
    clock = FakeClock(at("09:44", 50))
    feed = MockBroker(clock=clock)
    await feed.login()
    md = _md(feed, clock)
    key = InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)
    inst = await md.subscribe(key, lambda s, t: None)
    md.builder.track("NSE:NIFTY:INDEX:-:-", 5)
    feed.push("26000", 99)
    for sec, px in [(0, 100), (100, 108), (200, 97)]:
        clock.set(at("09:45") + timedelta(seconds=sec))
        feed.push("26000", px)
    clock.set(at("09:50", 1))
    feed.push("26000", 101)
    clock.set(at("09:50", 4))
    assert await md.get_candle(inst, at("09:45"), 5) is None  # waits a few seconds for the broker candle
    clock.set(at("09:50", 9))
    c = await md.get_candle(inst, at("09:45"), 5)
    assert (c.high, c.low) == (108, 97) and c.source == "ticks"


def test_evaluate_exit_priorities():
    st = ExitState(trade_db_id=1, trade_ref="T1", instance_id=1, instance_name="x", account_id=1, mode="paper",
                   direction="CE_BUY", spot_series="s", option_series="o",
                   option_key=InstrumentKey(underlying="NIFTY", exchange=Exchange.NFO, instrument_type=InstrumentType.CE),
                   spot_key=None, entry_price=100, quantity=65, exit_at=at("10:00"), square_off_at=at("15:15"),
                   spot_sl=23609.2, spot_sl_side="below", tp_spot_level=23700, tp_spot_side="above")
    assert evaluate_exit(st, 23650, 110, at("09:55")) is None
    assert evaluate_exit(st, 23609.2, 90, at("09:55")) == "spot_stop_loss"
    assert evaluate_exit(st, 23700, 150, at("09:55")) == "take_profit"
    assert evaluate_exit(st, 23650, 110, at("10:00")) == "timed_exit"
    st.exit_at = at("15:20")
    assert evaluate_exit(st, 23650, 110, at("15:15")) == "square_off"


async def test_risk_limits_block_entries(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "R", "risk": {"max_trades_per_day": 1}}])
    try:
        p = env.platform
        acc = p.accounts[env.account_id]
        rt = p.strategies.runtimes[env.instance_ids[0]]
        ok = p.risk.check_entry(instance_id=rt.instance_id, limits=rt.risk_limits, account=acc, mode="paper",
                                lots=5, quantity=325, premium=100)
        assert ok.ok
        big = p.risk.check_entry(instance_id=rt.instance_id, limits=InstanceRiskLimits(max_lots_per_order=2),
                                 account=acc, mode="paper", lots=5, quantity=325, premium=100)
        assert not big.ok and big.level == "instance"
        p.trades.trade_counts[rt.instance_id] = 1
        assert not p.risk.check_entry(instance_id=rt.instance_id, limits=rt.risk_limits, account=acc, mode="paper",
                                      lots=5, quantity=325, premium=100).ok
        p.trades.trade_counts[rt.instance_id] = 0
        acc.kill_switch = True
        d = p.risk.check_entry(instance_id=rt.instance_id, limits=rt.risk_limits, account=acc, mode="paper", lots=5,
                               quantity=325, premium=100)
        assert not d.ok and d.level == "account"
        acc.kill_switch = False
        p.risk.global_kill = True
        d = p.risk.check_entry(instance_id=rt.instance_id, limits=rt.risk_limits, account=acc, mode="paper", lots=5,
                               quantity=325, premium=100)
        assert not d.ok and d.level == "global"
        p.risk.global_kill = False
        p.settings.risk.max_daily_loss = 1000
        p.trades.realized_today[999] = (rt.instance_id, acc.id, -1500.0)
        d = p.risk.check_entry(instance_id=rt.instance_id, limits=rt.risk_limits, account=acc, mode="paper", lots=5,
                               quantity=325, premium=100)
        assert not d.ok and "daily loss" in d.reason
        # the global signal is also rejected end-to-end with a SIGNAL_REJECTED event
        env.ref_candle("09:45", 23639.20, 23609.20)
        env.spot(23630.0)
        env.option_price(23650, "CE", 100.0)
        await env.at("09:50", 6)
        await env.at("09:52", 0)
        env.spot(23660.0)
        await env.wait_for(lambda: env.types("SIGNAL_REJECTED"))
        assert not p.trades.live
    finally:
        await env.platform.stop()


def _day_candles(day: date, ref_start: str, high: float, low: float, breakout: float | None) -> list[Candle]:
    """Flat 1-minute candles at 100 with a reference candle and an optional breakout after it."""
    out = []
    t = datetime(day.year, day.month, day.day, 9, 15, tzinfo=at("09:15").tzinfo)
    rs = datetime.fromisoformat(f"{day.isoformat()}T{ref_start}:00").replace(tzinfo=t.tzinfo)
    while t.hour < 15 or (t.hour == 15 and t.minute < 30):
        o = h = l = c = 100.0
        if rs <= t < rs + timedelta(minutes=5):
            h, l = high, low
        if breakout is not None and t == rs + timedelta(minutes=6):
            o, h, l, c = 100.0, breakout, 100.0, breakout
        if breakout is not None and t > rs + timedelta(minutes=6):
            o = h = l = c = breakout
        out.append(Candle(start=t, timeframe_min=1, open=o, high=h, low=l, close=c))
        t += timedelta(minutes=1)
    return out


async def test_backtest_replays_strategy_rules():
    day = date(2026, 11, 3)
    candles = _day_candles(day, "09:45", 101.0, 99.0, breakout=110.0)
    cfg = BacktestConfig(template_id="nifty_ref_breakout",
                         params={"reference_candle_starts": "09:45", "strike_step": 50}, day_from=day, day_to=day)
    res = await run_backtest(cfg, candles)
    trades = res["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "CE_BUY" and t["trigger"] == 103.25 and t["spot_sl"] == 99.0
    assert t["exit_reason"] == "timed_exit" and t["exit_at"] == "10:00" and t["exit_time"].startswith("10:00")
    assert t["qty"] == 5 * 65
    assert res["summary"]["trades"] == 1 and res["summary"]["win_rate"] in (0.0, 100.0)


async def test_backtest_spot_sl_and_no_breakout():
    day = date(2026, 11, 3)
    candles = _day_candles(day, "09:45", 101.0, 99.0, breakout=None)
    cfg = BacktestConfig(template_id="nifty_ref_breakout", params={"reference_candle_starts": "09:45"},
                         day_from=day, day_to=day)
    res = await run_backtest(cfg, candles)
    assert res["trades"] == [] and any("SKIP slot=#1 reason=no_breakout" in e["message"] for e in res["events"])
    # breakout up, then collapse below ref low -> spot SL
    candles = _day_candles(day, "09:45", 101.0, 99.0, breakout=104.0)
    for c in candles:
        if c.start.hour == 9 and c.start.minute == 55:
            c.open, c.high, c.low, c.close = 104.0, 104.0, 98.0, 98.0
    res = await run_backtest(cfg, candles)
    assert res["trades"][0]["exit_reason"] == "spot_stop_loss"


def test_parse_candle_csv():
    text = "timestamp,open,high,low,close,volume\n2026-11-03T09:15:00+05:30,1,2,0.5,1.5,10\n2026-11-03 09:16:00,1.5,2,1,1.8,5\n"
    cs = parse_candle_csv(text)
    assert len(cs) == 2 and cs[0].high == 2 and cs[1].start.minute == 16 and cs[0].timeframe_min == 1
    with pytest.raises(ValueError):
        parse_candle_csv("a,b\n1,2\n")


def test_exit_manager_wakes_exactly_at_exit_time():
    from datetime import timedelta

    from brokers.base import Exchange, InstrumentKey, InstrumentType
    from core.clock import FakeClock
    from engine.exit_manager import ExitManager, ExitState
    from tests.conftest import at

    clock = FakeClock(at("09:59", 59))
    clock.set(at("09:59", 59) + timedelta(milliseconds=920))
    em = ExitManager(clock, lambda: 5.0)
    assert em._next_wait() == 0.25  # nothing open
    em.register(ExitState(trade_db_id=1, trade_ref="T", instance_id=1, instance_name="x", account_id=1, mode="paper",
                          direction="CE_BUY", spot_series="s", option_series="o",
                          option_key=InstrumentKey(underlying="NIFTY", exchange=Exchange.NFO,
                                                   instrument_type=InstrumentType.CE),
                          spot_key=None, entry_price=100, quantity=65, exit_at=at("10:00")))
    assert abs(em._next_wait() - 0.08) < 1e-6  # wakes at 10:00:00.000, not up to 250 ms later


class _SimFeed(MockBroker):
    """A mock account that reports synthetic prices, like a Paper account with simulate_market on."""

    @property
    def simulated_feed(self) -> bool:
        return True


async def _feeds(clock):
    from engine.market_data import FeedCandidate

    MockBroker.expiries = [DAY]
    real, sim = MockBroker(clock=clock), _SimFeed(clock=clock)
    await real.login()
    await sim.login()
    gs = GlobalSettings()
    md = MarketDataService(EventBus(), clock, MarketCalendar(gs), lambda: gs)
    md.candidates = lambda: [FeedCandidate(1, "Angel", real), FeedCandidate(2, "Paper sim", sim)]
    events = []
    md.bus.add_sink(events.append)
    return md, real, sim, events


async def test_real_feed_outage_never_fails_over_to_a_simulator():
    """06-Nov-2026: Angel's feed went quiet for 15 s and the platform switched to the simulated Paper account."""
    clock = FakeClock(at("11:35"))
    md, real, sim, events = await _feeds(clock)
    assert await md.ensure_feed() and md.feed is real
    assert await md.ensure_feed(exclude={1})  # what the stale-feed watchdog does
    assert md.feed is real  # stays on the real feed (it may recover) instead of fake prices
    assert any(e.event_type == "FEED_NO_FALLBACK" for e in events)
    real._connected = False  # real account fully down: no feed at all, rather than a fake one
    await md.ensure_feed()
    assert md.feed is None


async def test_simulated_feed_is_replaced_once_a_real_account_connects():
    clock = FakeClock(at("09:15"))
    md, real, sim, events = await _feeds(clock)
    real._connected = False
    await md.ensure_feed()
    assert md.feed is None  # a real account is set up, so the simulator is not used even at startup
    md.feed, md.feed_account_id, md.feed_label = sim, 2, "Paper sim"  # e.g. picked by an older version
    real._connected = True
    assert md._real_available()
    await md.ensure_feed()
    assert md.feed is real
    assert any(e.event_type == "FEED_SIMULATED_DROPPED" for e in events)


async def test_simulator_is_the_feed_when_no_real_account_exists():
    from engine.market_data import FeedCandidate

    clock = FakeClock(at("09:15"))
    MockBroker.expiries = [DAY]
    sim = _SimFeed(clock=clock)
    await sim.login()
    gs = GlobalSettings()
    md = MarketDataService(EventBus(), clock, MarketCalendar(gs), lambda: gs)
    md.candidates = lambda: [FeedCandidate(2, "Paper sim", sim)]
    assert await md.ensure_feed() and md.feed is sim  # demo setups still work
