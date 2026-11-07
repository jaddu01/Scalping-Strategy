"""Builds OHLC candles of several timeframes from ticks.

A candle is marked `complete` only if ticks were being received from before the candle
started (otherwise the open/high/low may be missing) — incomplete candles are replaced by the
broker's historical candle when one is requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from brokers.base import Candle, Tick
from core.clock import floor_to_timeframe, to_ist

CandleCallback = Callable[[str, Candle], None]  # (series key, closed candle)


@dataclass
class _Working:
    start: datetime
    o: float
    h: float
    l: float
    c: float
    v: float
    complete: bool


class CandleBuilder:
    def __init__(self, close_grace_sec: float = 1.5, history: int = 400) -> None:
        self.close_grace = timedelta(seconds=close_grace_sec)
        self.history = history
        self._series_tf: dict[str, set[int]] = {}
        self._working: dict[tuple[str, int], _Working] = {}
        self._closed: dict[tuple[str, int], dict[datetime, Candle]] = {}
        self._since: dict[tuple[str, int], datetime] = {}  # first tick seen per (series, timeframe) since tracking
        self._listeners: list[CandleCallback] = []

    def add_listener(self, cb: CandleCallback) -> None:
        self._listeners.append(cb)

    def track(self, series: str, timeframe_min: int) -> None:
        # a timeframe added mid-candle must not mark that first partial candle complete, so the
        # "ticks seen since" clock is kept per (series, timeframe), starting when tracking begins
        self._series_tf.setdefault(series, set()).add(timeframe_min)

    def reset_series(self, series: str) -> None:
        """Called when the feed reconnects/switches: candles in progress can no longer be trusted."""
        for key in [k for k in self._since if k[0] == series]:
            self._since.pop(key, None)
        for key in [k for k in self._working if k[0] == series]:
            self._working[key].complete = False

    def on_tick(self, series: str, tick: Tick) -> None:
        tfs = self._series_tf.get(series)
        if not tfs:
            return
        ts = to_ist(tick.ts)
        for tf in tfs:
            first = self._since.setdefault((series, tf), ts)
            start = floor_to_timeframe(ts, tf)
            key = (series, tf)
            w = self._working.get(key)
            if w is not None and start < w.start:
                continue  # late/out-of-order tick for an already closed candle
            if w is not None and start > w.start:
                self._close(key, w)
                w = None
            if w is None:
                complete = first <= start + timedelta(seconds=2)
                self._working[key] = _Working(start, tick.ltp, tick.ltp, tick.ltp, tick.ltp, float(tick.volume or 0),
                                              complete)
            else:
                w.h = max(w.h, tick.ltp)
                w.l = min(w.l, tick.ltp)
                w.c = tick.ltp

    def on_time(self, now: datetime) -> None:
        """Close candles whose period has ended even if no new tick arrived."""
        for key, w in list(self._working.items()):
            if now >= w.start + timedelta(minutes=key[1]) + self.close_grace:
                self._close(key, w)

    def _close(self, key: tuple[str, int], w: _Working) -> None:
        self._working.pop(key, None)
        candle = Candle(start=w.start, timeframe_min=key[1], open=w.o, high=w.h, low=w.l, close=w.c, volume=w.v,
                        complete=w.complete, source="ticks")
        store = self._closed.setdefault(key, {})
        store[w.start] = candle
        if len(store) > self.history:
            for old in sorted(store)[: len(store) - self.history]:
                del store[old]
        for cb in list(self._listeners):
            cb(key[0], candle)

    def get(self, series: str, start: datetime, timeframe_min: int) -> Candle | None:
        return self._closed.get((series, timeframe_min), {}).get(to_ist(start))

    def working(self, series: str, timeframe_min: int) -> Candle | None:
        w = self._working.get((series, timeframe_min))
        if w is None:
            return None
        return Candle(start=w.start, timeframe_min=timeframe_min, open=w.o, high=w.h, low=w.l, close=w.c,
                      complete=False, source="ticks")


def candles_match(a: Candle, b: Candle, tolerance: float) -> bool:
    return (abs(a.high - b.high) <= tolerance and abs(a.low - b.low) <= tolerance
            and abs(a.open - b.open) <= tolerance and abs(a.close - b.close) <= tolerance)
