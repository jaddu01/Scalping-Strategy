"""Shared market-data service.

One designated "data feed" broker account streams ticks for every symbol any instance needs;
each symbol is subscribed once, however many instances use it. Subscriptions are keyed by a
broker-independent series id (exchange/underlying/type/expiry/strike), so the feed can fall
back to another connected account — even of a different broker — without strategies noticing.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from brokers.base import BrokerAdapter, Candle, Instrument, InstrumentKey, Tick
from core.clock import Clock, fmt_hm
from core.events import Category, EventBus, Level
from engine.calendar import MarketCalendar
from engine.candles import CandleBuilder, candles_match

log = logging.getLogger("algo.marketdata")

TickListener = Callable[[str, Tick], None]
CandleListener = Callable[[str, Candle], None]


def series_of(x: Instrument | InstrumentKey) -> str:
    exp = x.expiry.isoformat() if x.expiry else "-"
    strike = f"{x.strike:g}" if x.strike is not None else "-"
    return f"{x.exchange.value}:{x.underlying.upper()}:{x.instrument_type.value}:{exp}:{strike}"


def key_of(inst: Instrument) -> InstrumentKey:
    return InstrumentKey(underlying=inst.underlying, exchange=inst.exchange, instrument_type=inst.instrument_type,
                         expiry=inst.expiry, strike=inst.strike)


@dataclass
class _Sub:
    key: InstrumentKey
    instrument: Instrument | None = None
    listeners: list[TickListener] = field(default_factory=list)


@dataclass
class FeedCandidate:
    account_id: int
    label: str
    adapter: BrokerAdapter
    preferred: bool = False


def _simulated(adapter: Any) -> bool:
    return bool(getattr(adapter, "simulated_feed", False))


class MarketDataService:
    def __init__(self, bus: EventBus, clock: Clock, calendar: MarketCalendar,
                 settings_getter: Callable[[], Any]) -> None:
        self.bus = bus
        self.clock = clock
        self.calendar = calendar
        self.settings = settings_getter
        self.feed: BrokerAdapter | None = None
        self.feed_account_id: int | None = None
        self.feed_label: str = ""
        self.candidates: Callable[[], list[FeedCandidate]] = lambda: []
        self._subs: dict[str, _Sub] = {}
        self._token_series: dict[tuple[str, str], str] = {}
        self._ltp: dict[str, tuple[float, datetime]] = {}
        self.last_tick_at: datetime | None = None
        self.builder = CandleBuilder()
        self.builder.add_listener(self._on_candle_closed)
        self._candle_listeners: dict[str, list[CandleListener]] = {}
        self._candle_cache: dict[tuple[str, datetime, int], Candle] = {}
        self._raw_listeners: list[Callable[[str, InstrumentKey, Tick], None]] = []
        self._stale_reported = False
        self._attached_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._switch_lock = asyncio.Lock()

    # ------------------------------------------------------------------ feed selection
    def _emit(self, event_type: str, message: str, level: Level = Level.INFO, **payload: Any) -> None:
        self.bus.emit(Category.SYSTEM, event_type, message, level, account_id=self.feed_account_id,
                      account_label=self.feed_label or None, **payload)

    async def ensure_feed(self, exclude: set[int] | None = None) -> bool:
        """Pick (or re-pick) the feed account. Returns True if a feed is active.

        A simulated paper account (synthetic prices) is only ever a feed when no real broker account
        is set up at all. With a real account configured it is never used, not even as a fallback:
        on 06-Nov-2026 a 15 s internet drop made the platform fail over to the simulator and the
        strategy then read a simulated NIFTY ~400 points away from the real one."""
        async with self._switch_lock:
            exclude = exclude or set()
            everyone = self.candidates()
            real_configured = any(not _simulated(c.adapter) and getattr(c.adapter, "supports_tick_stream", False)
                                  for c in everyone)
            cands = [c for c in everyone if c.adapter.is_connected()
                     and getattr(c.adapter, "supports_tick_stream", False) and c.account_id not in exclude
                     and not (real_configured and _simulated(c.adapter))]
            if self.feed is not None and real_configured and _simulated(self.feed):
                self._emit("FEED_SIMULATED_DROPPED", f"{self.feed_label} is simulated; not used while a real account "
                           "is set up", Level.WARNING)
                self._detach()
            if self.feed is not None and self.feed.is_connected() and self.feed_account_id not in exclude:
                preferred = self.settings().general.data_feed_account_id
                if preferred is None or preferred == self.feed_account_id or not any(
                        c.account_id == preferred for c in cands):
                    return True
            if not cands and exclude and self.feed is not None and self.feed.is_connected():
                self._emit("FEED_NO_FALLBACK", f"no other account can stream data; staying on {self.feed_label}",
                           Level.WARNING)
                return True
            if not cands:
                if self.feed is not None:
                    self._emit("FEED_UNAVAILABLE", "no connected account can stream market data", Level.ERROR)
                self._detach()
                return False
            preferred_id = self.settings().general.data_feed_account_id
            cands.sort(key=lambda c: (c.account_id != preferred_id, c.adapter.is_paper, c.account_id))
            chosen = cands[0]
            if not self.settings().general.feed_fallback and preferred_id is not None and chosen.account_id != preferred_id:
                self._emit("FEED_UNAVAILABLE", "designated feed account unavailable and fallback is off", Level.ERROR)
                self._detach()
                return False
            await self._attach(chosen)
            return True

    def _detach(self) -> None:
        if self.feed is not None:
            self.feed.remove_tick_listener(self._on_feed_tick)
        self.feed, self.feed_account_id, self.feed_label = None, None, ""

    async def _attach(self, c: FeedCandidate) -> None:
        old = self.feed_label
        self._detach()
        self.feed, self.feed_account_id, self.feed_label = c.adapter, c.account_id, c.label
        c.adapter.add_tick_listener(self._on_feed_tick)
        self._token_series.clear()
        for series in list(self._subs):
            self.builder.reset_series(series)
        resolved = []
        for series, sub in self._subs.items():
            try:
                sub.instrument = c.adapter.resolve_symbol(sub.key)
                self._token_series[sub.instrument.key] = series
                resolved.append(sub.instrument)
            except Exception as exc:
                self._emit("FEED_RESOLVE_FAILED", f"{sub.key.label()}: {exc}", Level.WARNING)
        if resolved:
            await c.adapter.subscribe_ticks(resolved)
        self.last_tick_at = None
        self._attached_at = self.clock.now()
        self._stale_reported = False
        if old and old != c.label:
            self._emit("FEED_SWITCHED", f"data feed switched from {old} to {c.label}", Level.WARNING,
                       from_account=old, to_account=c.label)
        else:
            self._emit("FEED_SELECTED", f"data feed account: {c.label}", feed=c.label, symbols=len(resolved))

    # ------------------------------------------------------------------ subscriptions
    def resolve(self, key: InstrumentKey) -> Instrument:
        if self.feed is None:
            raise RuntimeError("no market data feed is connected")
        return self.feed.resolve_symbol(key)

    def option_expiries(self, underlying: str, exchange: Any = None) -> list:
        if self.feed is None:
            return []
        return self.feed.option_expiries(underlying, exchange) if exchange is not None else self.feed.option_expiries(underlying)

    async def subscribe(self, key: InstrumentKey, listener: TickListener | None = None) -> Instrument | None:
        series = series_of(key)
        sub = self._subs.get(series)
        new = sub is None
        if sub is None:
            sub = self._subs[series] = _Sub(key=key)
        if listener is not None and listener not in sub.listeners:
            sub.listeners.append(listener)
        if self.feed is None:
            await self.ensure_feed()
        if self.feed is not None and (new or sub.instrument is None):
            sub.instrument = self.feed.resolve_symbol(key)
            self._token_series[sub.instrument.key] = series
            await self.feed.subscribe_ticks([sub.instrument])
            self._emit("SUBSCRIBED", f"{sub.instrument.tradingsymbol}", Level.DEBUG, series=series)
        return sub.instrument

    async def unsubscribe(self, key: InstrumentKey, listener: TickListener | None = None) -> None:
        series = series_of(key)
        sub = self._subs.get(series)
        if sub is None:
            return
        if listener in sub.listeners:
            sub.listeners.remove(listener)
        if not sub.listeners and not self._candle_listeners.get(series):
            self._subs.pop(series, None)
            if sub.instrument is not None and self.feed is not None:
                self._token_series.pop(sub.instrument.key, None)
                try:
                    await self.feed.unsubscribe_ticks([sub.instrument])
                except Exception:
                    pass

    def add_candle_listener(self, key: InstrumentKey, timeframe_min: int, listener: CandleListener) -> None:
        series = series_of(key)
        self.builder.track(series, timeframe_min)
        lst = self._candle_listeners.setdefault(series, [])
        if listener not in lst:
            lst.append(listener)

    def remove_candle_listener(self, key: InstrumentKey, listener: CandleListener) -> None:
        lst = self._candle_listeners.get(series_of(key), [])
        if listener in lst:
            lst.remove(listener)

    def add_raw_listener(self, cb: Callable[[str, InstrumentKey, Tick], None]) -> None:
        """Every tick with its series + normalized key (used by paper brokers and the exit manager)."""
        self._raw_listeners.append(cb)

    def remove_raw_listener(self, cb: Callable[[str, InstrumentKey, Tick], None]) -> None:
        if cb in self._raw_listeners:
            self._raw_listeners.remove(cb)

    # ------------------------------------------------------------------ ticks
    def _on_feed_tick(self, tick: Tick) -> None:
        series = self._token_series.get((tick.exchange.value, tick.token))
        if series is None:
            return
        now = self.clock.now()
        self.last_tick_at = now
        if self._stale_reported:
            self._stale_reported = False
            self._emit("FEED_RECOVERED", "market data ticks flowing again")
        self._ltp[series] = (tick.ltp, tick.ts)
        self.builder.on_tick(series, tick)
        sub = self._subs.get(series)
        if sub is None:
            return
        for cb in list(sub.listeners):
            try:
                cb(series, tick)
            except Exception:
                log.exception("tick listener failed")
        for cb in list(self._raw_listeners):
            try:
                cb(series, sub.key, tick)
            except Exception:
                log.exception("raw tick listener failed")

    def ltp(self, x: Instrument | InstrumentKey | str) -> float | None:
        series = x if isinstance(x, str) else series_of(x)
        v = self._ltp.get(series)
        return v[0] if v else None

    def ltp_age(self, x: Instrument | InstrumentKey) -> float | None:
        v = self._ltp.get(series_of(x))
        if not v:
            return None
        return (self.clock.now() - v[1]).total_seconds()

    def seed_ltp(self, x: Instrument | InstrumentKey, price: float) -> None:
        if price:
            self._ltp[series_of(x)] = (price, self.clock.now())

    async def fetch_ltp(self, key: InstrumentKey) -> float | None:
        """Live price from the stream if fresh, otherwise a REST quote from the feed account."""
        p = self.ltp(key)
        age = self.ltp_age(key)
        if p is not None and age is not None and age < 5:
            return p
        if self.feed is None:
            return p
        try:
            inst = self.feed.resolve_symbol(key)
            quotes = await self.feed.get_ltp([inst])
            q = quotes.get(inst.key)
            if q:
                self.seed_ltp(key, q)
                return q
        except Exception as exc:
            log.warning("REST ltp failed for %s: %s", key.label(), exc)
        return p

    # ------------------------------------------------------------------ candles
    def _on_candle_closed(self, series: str, candle: Candle) -> None:
        for cb in list(self._candle_listeners.get(series, [])):
            try:
                cb(series, candle)
            except Exception:
                log.exception("candle listener failed")

    async def history(self, inst: Instrument, timeframe_min: int, count: int) -> list[Candle]:
        """Last `count` completed candles from the feed account's historical API (oldest first)."""
        if self.feed is None or count <= 0:
            return []
        now = self.clock.now()
        trading_days = -(-count * timeframe_min // 375) + 1
        start = now - timedelta(days=trading_days * 2 + 4)  # room for weekends and holidays
        try:
            feed_inst = self.feed.resolve_symbol(key_of(inst))
            rows = await self.feed.get_historical_candles(feed_inst, timeframe_min, start, now)
        except Exception as exc:
            self._emit("HISTORICAL_FETCH_FAILED", f"history {inst.tradingsymbol} {timeframe_min}m: {exc}", Level.WARNING)
            return []
        done = sorted((c for c in rows if c.start + timedelta(minutes=timeframe_min) <= now), key=lambda c: c.start)
        return done[-count:]

    async def get_candle(self, inst: Instrument, start: datetime, timeframe_min: int) -> Candle | None:
        """Reference candle: built from ticks, cross-checked with the broker's historical API.
        The broker's candle wins on mismatch (broker is the source of truth)."""
        series = series_of(inst)
        ck = (series, start, timeframe_min)
        if ck in self._candle_cache:
            return self._candle_cache[ck]
        built = self.builder.get(series, start, timeframe_min)
        hist: Candle | None = None
        if self.feed is not None:
            try:
                feed_inst = self.feed.resolve_symbol(key_of(inst))
                rows = await self.feed.get_historical_candles(
                    feed_inst, timeframe_min, start, start + timedelta(minutes=timeframe_min) - timedelta(seconds=1))
                hist = next((c for c in rows if c.start == start), None)
            except Exception as exc:
                self._emit("HISTORICAL_FETCH_FAILED", f"{fmt_hm(start)} candle: {exc}", Level.DEBUG)
        tol = self.settings().sync.candle_tolerance_points
        result: Candle | None
        if hist is not None and built is not None and built.complete:
            if not candles_match(built, hist, tol):
                self._emit("CANDLE_MISMATCH",
                           f"{inst.tradingsymbol} {fmt_hm(start)} ticks H={built.high} L={built.low} vs broker "
                           f"H={hist.high} L={hist.low}; using broker candle", Level.WARNING,
                           built=built.model_dump(mode="json"), historical=hist.model_dump(mode="json"))
            result = hist
        elif hist is not None:
            result = hist
        elif built is not None and built.complete:
            # historical API may lag; accept the tick candle once it is a few seconds old
            age = (self.clock.now() - (start + timedelta(minutes=timeframe_min))).total_seconds()
            result = built if age >= 8 else None
        else:
            result = None
        if result is not None:
            self._candle_cache[ck] = result
            if len(self._candle_cache) > 2000:
                for k in list(self._candle_cache)[:500]:
                    del self._candle_cache[k]
        return result

    # ------------------------------------------------------------------ monitor
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._monitor(), name="market-data-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._detach()

    async def _monitor(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.5)
                now = self.clock.now()
                self.builder.on_time(now)
                if self.feed is None or not self.feed.is_connected() or _simulated(self.feed):
                    # no feed, a dead one, or a simulator that a real account may now replace
                    if self._subs and (not _simulated(self.feed) or self._real_available()):
                        await self.ensure_feed()
                    if self.feed is None or not self.feed.is_connected():
                        continue
                if not self._subs or not self.calendar.is_market_open(now):
                    continue
                threshold = self.settings().sync.tick_stale_sec
                opened = self.calendar.at(now.date(), self.settings().market.market_open)
                since = max(self._attached_at or now, opened)
                ref = self.last_tick_at if self.last_tick_at and self.last_tick_at > since else since
                age = (now - ref).total_seconds()
                if age > threshold and not self._stale_reported:
                    self._stale_reported = True
                    self._emit("FEED_STALE", f"no ticks for {age:.0f}s from {self.feed_label}", Level.ERROR,
                               age_sec=round(age, 1))
                    if self.settings().general.feed_fallback:
                        await self.ensure_feed(exclude={self.feed_account_id} if self.feed_account_id else None)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market data monitor error")

    def _real_available(self) -> bool:
        return any(c.adapter.is_connected() and not _simulated(c.adapter)
                   and getattr(c.adapter, "supports_tick_stream", False) for c in self.candidates())

    def is_stale(self) -> bool:
        return self._stale_reported

    def status(self) -> dict[str, Any]:
        return {
            "feed_account_id": self.feed_account_id,
            "feed_label": self.feed_label,
            "connected": bool(self.feed and self.feed.is_connected()),
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "stale": self._stale_reported,
            "symbols": len(self._subs),
        }
