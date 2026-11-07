"""On-disk cache of historical 1-minute spot candles, one CSV per index per trading day.

Backtests that use the broker as the data source read finished days from here and only ask the
broker for days that are missing, so a re-run uses exactly the same data and can be compared.
Today's (incomplete) day is never cached. Files can also be downloaded for offline use; they are in
the same format the CSV upload accepts.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from brokers.base import Candle
from core.clock import to_ist
from backtest.engine import parse_candle_csv

FIELDS = ["timestamp", "open", "high", "low", "close", "volume"]


class CandleCache:
    def __init__(self, root: Path) -> None:
        self.root = root / "candles"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, exchange: str, underlying: str, day: date) -> Path:
        return self.root / f"{exchange.upper()}_{underlying.upper()}_{day.isoformat()}.csv"

    def load_day(self, exchange: str, underlying: str, day: date) -> list[Candle] | None:
        p = self.path(exchange, underlying, day)
        if not p.exists():
            return None
        try:
            return parse_candle_csv(p.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def save_day(self, exchange: str, underlying: str, day: date, candles: list[Candle]) -> None:
        if not candles:
            return
        p = self.path(exchange, underlying, day)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(to_csv(candles), encoding="utf-8")
        tmp.replace(p)

    async def get_range(self, exchange: str, underlying: str, day_from: date, day_to: date, today: date,
                        fetch: Callable[[datetime, datetime], Awaitable[list[Candle]]],
                        start_of: Callable[[date], datetime], end_of: Callable[[date], datetime]
                        ) -> tuple[list[Candle], int, int]:
        """Returns (candles, days_from_cache, days_fetched). Weekends are skipped; holidays simply
        come back empty from the broker and are not cached."""
        days = [day_from + timedelta(days=i) for i in range((day_to - day_from).days + 1)]
        days = [d for d in days if d.weekday() < 5]
        out: list[Candle] = []
        missing: list[date] = []
        cached = 0
        for d in days:
            rows = self.load_day(exchange, underlying, d) if d < today else None
            if rows:
                out.extend(rows)
                cached += 1
            else:
                missing.append(d)
        fetched = 0
        if missing:
            rows = await fetch(start_of(missing[0]), end_of(missing[-1]))
            by_day: dict[date, list[Candle]] = {}
            for c in rows:
                by_day.setdefault(to_ist(c.start).date(), []).append(c)
            for d in missing:
                day_rows = by_day.get(d, [])
                if day_rows:
                    fetched += 1
                    out.extend(day_rows)
                    if d < today:
                        self.save_day(exchange, underlying, d, day_rows)
        out.sort(key=lambda c: c.start)
        return out, cached, fetched

    def export_range(self, exchange: str, underlying: str, day_from: date, day_to: date) -> str:
        rows: list[Candle] = []
        for i in range((day_to - day_from).days + 1):
            rows.extend(self.load_day(exchange, underlying, day_from + timedelta(days=i)) or [])
        return to_csv(rows)


def to_csv(candles: list[Candle]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FIELDS)
    for c in sorted(candles, key=lambda c: c.start):
        w.writerow([to_ist(c.start).isoformat(), c.open, c.high, c.low, c.close, c.volume])
    return buf.getvalue()
