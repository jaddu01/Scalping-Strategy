"""Time helpers. Every timestamp in the platform is timezone-aware Asia/Kolkata."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def fmt_hms(dt: datetime) -> str:
    return to_ist(dt).strftime("%H:%M:%S")


def fmt_hms_ms(dt: datetime) -> str:
    dt = to_ist(dt)
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def fmt_hm(dt: datetime | time) -> str:
    return dt.strftime("%H:%M")


def hm_or_hms(dt: datetime | time) -> str:
    """HH:MM, or HH:MM:SS when the time is not on a whole minute (e.g. a 09:44:58 timed exit)."""
    return dt.strftime("%H:%M:%S" if dt.second else "%H:%M")


def parse_hhmm(value: str) -> time:
    value = value.strip()
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid time {value!r}, expected HH:MM")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) == 3 else 0
    return time(h, m, s)


def floor_to_timeframe(dt: datetime, minutes: int) -> datetime:
    """Start of the candle (aligned to 09:15 session for NSE, which is aligned to 5/15 minute marks)."""
    dt = to_ist(dt)
    total = dt.hour * 60 + dt.minute
    start = total - (total % minutes)
    return dt.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)


def format_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 0:
        secs = 0
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


class Clock:
    """Real clock. Engine code takes a Clock so tests and backtests can substitute time."""

    def now(self) -> datetime:
        return now_ist()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, when: datetime) -> None:
        delay = (when - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


class FakeClock(Clock):
    """Manually advanced clock for tests and backtests."""

    def __init__(self, start: datetime):
        self._now = to_ist(start)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = to_ist(when)

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)

    async def sleep_until(self, when: datetime) -> None:
        if when > self._now:
            self._now = when
        await asyncio.sleep(0)
