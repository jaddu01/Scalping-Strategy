"""Trading calendar: weekends, NSE holidays and market phases."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time

import yaml

from core.clock import combine, to_ist
from core.settings_schema import GlobalSettings


class MarketCalendar:
    def __init__(self, settings: GlobalSettings, holidays: dict[date, str] | None = None) -> None:
        self.settings = settings
        self.holidays = dict(holidays or {})

    def update(self, settings: GlobalSettings | None = None, holidays: dict[date, str] | None = None) -> None:
        if settings is not None:
            self.settings = settings
        if holidays is not None:
            self.holidays = dict(holidays)

    def is_trading_day(self, d: date) -> bool:
        if d in self.holidays:
            return False
        if d.weekday() >= 5 and not self.settings.general.trade_on_weekends:
            return False
        return True

    def holiday_name(self, d: date) -> str | None:
        return self.holidays.get(d)

    def at(self, d: date, t: time) -> datetime:
        return combine(d, t)

    def is_market_open(self, now: datetime) -> bool:
        now = to_ist(now)
        m = self.settings.market
        return self.is_trading_day(now.date()) and m.market_open <= now.time() < m.market_close

    def in_session_window(self, now: datetime) -> bool:
        """Pre-open to close: the window in which strategies are driven."""
        now = to_ist(now)
        m = self.settings.market
        return self.is_trading_day(now.date()) and m.pre_open <= now.time() <= m.market_close

    def square_off_time(self) -> time:
        return self.settings.market.final_square_off

    def upcoming_holidays(self, today: date, n: int = 5) -> list[tuple[date, str]]:
        return [(d, n_) for d, n_ in sorted(self.holidays.items()) if d >= today][:n]


def parse_holiday_file(text: str) -> dict[date, str]:
    """Accepts CSV (date,description) with dates as YYYY-MM-DD or DD-Mon-YYYY, or YAML
    (list of {date, description} or a {date: description} mapping)."""
    text = text.strip()
    if not text:
        return {}
    out: dict[date, str] = {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        for k, v in data.items():
            out[_parse_date(str(k))] = str(v or "")
        return out
    if isinstance(data, list) and data and isinstance(data[0], dict):
        for row in data:
            out[_parse_date(str(row.get("date")))] = str(row.get("description", "") or "")
        return out
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip() or row[0].strip().lower() in ("date", "day"):
            continue
        cells = [c.strip() for c in row]
        d = None
        for c in cells:
            try:
                d = _parse_date(c)
                break
            except ValueError:
                continue
        if d is None:
            raise ValueError(f"no date found in row {row}")
        desc = next((c for c in cells if c and not _is_date(c) and not c.isdigit()), "")
        out[d] = desc
    return out


def _is_date(s: str) -> bool:
    try:
        _parse_date(s)
        return True
    except ValueError:
        return False


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%B-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")
