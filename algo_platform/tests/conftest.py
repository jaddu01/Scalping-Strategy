from __future__ import annotations

import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALGO_MASTER_KEY", Fernet.generate_key().decode())

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, InstrumentType  # noqa: E402
from core.clock import FakeClock, combine  # noqa: E402
from core.events import Category, Level  # noqa: E402
from strategies.base import EntrySignal, TradeInfo  # noqa: E402

DAY = date(2026, 11, 5)  # a Thursday
NEXT_TUE = date(2026, 11, 10)


def at(hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return combine(DAY, time(h, m, sec))


class FakeStrategyContext:
    """Minimal StrategyContext for strategy unit tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.instance_id = 1
        self.instance_name = "TEST"
        self.clock = FakeClock(start or at("09:30"))
        self.events: list[dict[str, Any]] = []
        self.signals: list[EntrySignal] = []
        self.candles: dict[datetime, Candle] = {}
        self.spot_ltp: float | None = None
        self.open_position = False
        self.reject_next = False
        self.expiries = [DAY, NEXT_TUE]
        self.spot = Instrument(exchange=Exchange.NSE, token="99926000", tradingsymbol="Nifty 50",
                               underlying="NIFTY", instrument_type=InstrumentType.INDEX)
        self._trade_seq = 0

    def now(self) -> datetime:
        return self.clock.now()

    def emit(self, event_type: str, message: str, level: Level = Level.INFO,
             category: Category = Category.STRATEGY, trade_id: str | None = None, **payload: Any) -> None:
        self.events.append({"type": event_type, "message": message, "level": level, "payload": payload})

    def resolve(self, key: InstrumentKey) -> Instrument:
        return self.spot

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        self.last_expiry_exchange = exchange
        return self.expiries

    def ltp(self, instrument: Instrument) -> float | None:
        return self.spot_ltp

    async def get_candle(self, instrument: Instrument, start: datetime, timeframe_min: int) -> Candle | None:
        return self.candles.get(start)

    async def submit_entry(self, signal: EntrySignal) -> TradeInfo | None:
        self.signals.append(signal)
        if self.reject_next:
            self.reject_next = False
            return None
        self._trade_seq += 1
        self.open_position = True
        return TradeInfo(trade_id=f"T{self._trade_seq}", slot_id=signal.slot_id, direction=signal.direction.value,
                         status="OPEN")

    def has_open_position(self) -> bool:
        return self.open_position

    def square_off_time(self) -> time:
        return time(15, 15)

    def messages(self, event_type: str) -> list[str]:
        return [e["message"] for e in self.events if e["type"] == event_type]


def make_candle(start: datetime, o: float, h: float, l: float, c: float, tf: int = 5) -> Candle:
    return Candle(start=start, timeframe_min=tf, open=o, high=h, low=l, close=c)


@pytest.fixture
def fake_ctx() -> FakeStrategyContext:
    return FakeStrategyContext()
