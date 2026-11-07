"""Structured event log: every event is a record with timestamp, level, category, type,
instance, account, trade correlation id, message and JSON payload.

The EventBus fans each event out to sinks (database writer, rotating JSONL file, console,
live UI hub, Telegram notifier). emit() never blocks and never raises.
"""
from __future__ import annotations

import itertools
import json
import logging
import logging.handlers
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from core.clock import fmt_hms, fmt_hms_ms, now_ist
from core.masking import mask_secrets, mask_text

log = logging.getLogger("algo")


class Level(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}[self.value]


class Category(str, Enum):
    SYSTEM = "SYSTEM"
    MARKET = "MARKET"
    STRATEGY = "STRATEGY"
    ORDER = "ORDER"
    POSITION = "POSITION"
    EXIT = "EXIT"
    RISK = "RISK"
    ERROR = "ERROR"
    AUDIT = "AUDIT"


_seq = itertools.count(1)


@dataclass
class Event:
    category: Category
    event_type: str
    message: str
    level: Level = Level.INFO
    ts: datetime = field(default_factory=now_ist)
    instance_id: int | None = None
    instance_name: str | None = None
    account_id: int | None = None
    account_label: str | None = None
    trade_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = field(default_factory=lambda: next(_seq))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts.isoformat(timespec="milliseconds"),
            "time": fmt_hms_ms(self.ts),
            "level": self.level.value,
            "category": self.category.value,
            "event_type": self.event_type,
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "account_id": self.account_id,
            "account_label": self.account_label,
            "trade_id": self.trade_id,
            "message": self.message,
            "payload": self.payload,
            "line": format_line(self),
        }


def format_line(ev: Event) -> str:
    """Platform log line, e.g. `[09:52:31.408] STRATEGY SIGNAL inst=NIFTY-Angel1 trade=T123 ...`."""
    parts = [f"[{fmt_hms_ms(ev.ts)}]", f"{ev.category.value:<8}", ev.event_type]
    if ev.instance_name and "inst=" not in ev.message:
        parts.append(f"inst={ev.instance_name}")
    if ev.trade_id and "trade=" not in ev.message:
        parts.append(f"trade={ev.trade_id}")
    if ev.message:
        parts.append(ev.message)
    return " ".join(parts)


def format_strategy_line(ev: Event) -> str:
    """Strategy log line, e.g. `[09:50:05] [NIFTY-Angel1] REF slot=#1 candle=09:45 ...`."""
    return f"[{fmt_hms(ev.ts)}] [{ev.instance_name or '-'}] {ev.message}"


Sink = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def publish(self, ev: Event) -> Event:
        ev.payload = mask_secrets(ev.payload)
        ev.message = mask_text(ev.message)
        for sink in list(self._sinks):
            try:
                sink(ev)
            except Exception:  # a broken sink must never break the caller
                log.exception("event sink failed")
        return ev

    def emit(
        self,
        category: Category,
        event_type: str,
        message: str = "",
        level: Level = Level.INFO,
        *,
        instance_id: int | None = None,
        instance_name: str | None = None,
        account_id: int | None = None,
        account_label: str | None = None,
        trade_id: str | None = None,
        **payload: Any,
    ) -> Event:
        ev = Event(
            category=category,
            event_type=event_type,
            message=message,
            level=level,
            instance_id=instance_id,
            instance_name=instance_name,
            account_id=account_id,
            account_label=account_label,
            trade_id=trade_id,
            payload=_jsonable(payload),
        )
        return self.publish(ev)

    def exception(self, event_type: str, message: str, exc: BaseException, **kw: Any) -> Event:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return self.emit(Category.ERROR, event_type, f"{message}: {exc!r}", Level.ERROR, stack=tb, **kw)


def _jsonable(obj: Any) -> Any:
    """Make payload JSON serialisable (datetimes, enums, decimals, pydantic models)."""
    try:
        return json.loads(json.dumps(obj, default=_default))
    except Exception:
        return {"repr": repr(obj)}


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat(timespec="milliseconds")
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


class JsonlFileSink:
    """Rotating JSON-lines file (one file per day, kept for `retention_days`)."""

    def __init__(self, directory: Path, retention_days: int = 30) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("algo.events.jsonl")
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)
        for h in list(self._logger.handlers):
            self._logger.removeHandler(h)
        self.handler = logging.handlers.TimedRotatingFileHandler(
            directory / "events.jsonl", when="midnight", backupCount=retention_days, encoding="utf-8"
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self.handler)

    def set_retention(self, days: int) -> None:
        self.handler.backupCount = days

    def __call__(self, ev: Event) -> None:
        self._logger.info(json.dumps(ev.to_dict(), ensure_ascii=False))

    def close(self) -> None:
        self.handler.close()


class ConsoleSink:
    def __init__(self, min_level: Level = Level.INFO) -> None:
        self.min_level = min_level
        self._logger = logging.getLogger("algo.events")

    def __call__(self, ev: Event) -> None:
        if ev.level.rank >= self.min_level.rank:
            self._logger.log(ev.level.rank, format_line(ev))
