"""A controllable in-memory broker for engine and contract tests.

Behaves like a real broker account: it has an instrument master, streams ticks you push with
`push()`, serves historical candles you register, and fills orders (via the paper engine).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar

from brokers.base import BrokerCredentials, Candle, Exchange, Instrument, InstrumentType, Profile, Tick
from brokers.instruments import InstrumentStore
from brokers.paper import PaperBroker, PaperCredentials
from core.clock import Clock

SPOT_TOKEN = "26000"
SENSEX_TOKEN = "99919000"


class MockCredentials(PaperCredentials):
    client_id: str = "MOCK1"


class MockBroker(PaperBroker):
    broker_type: ClassVar[str] = "mock"
    display_name: ClassVar[str] = "Mock broker (tests)"
    credentials_model: ClassVar[type[BrokerCredentials]] = MockCredentials
    is_paper: ClassVar[bool] = False  # behaves like a real account
    order_tag_max_len: ClassVar[int] = 20

    expiries: ClassVar[list[date]] = []
    lot_size: ClassVar[int] = 65

    def __init__(self, credentials: MockCredentials | None = None, *, clock: Clock | None = None, **kw: Any) -> None:
        super().__init__(credentials or MockCredentials(), clock=clock, **kw)
        self.supports_tick_stream = True  # type: ignore[misc]
        self.store = InstrumentStore()
        self.history: dict[datetime, Candle] = {}
        self.fail_login = False
        self.logins = 0
        self._build()

    def _build(self) -> None:
        self.store.add(Instrument(exchange=Exchange.NSE, token=SPOT_TOKEN, tradingsymbol="Nifty 50",
                                  underlying="NIFTY", instrument_type=InstrumentType.INDEX))
        tok = 50000
        for exp in self.expiries:
            for k in range(23000, 24501, 50):
                for t in (InstrumentType.CE, InstrumentType.PE):
                    tok += 1
                    self.store.add(Instrument(exchange=Exchange.NFO, token=str(tok),
                                              tradingsymbol=f"NIFTY{exp:%d%b%y}{k}{t.value}".upper(),
                                              underlying="NIFTY", instrument_type=t, expiry=exp, strike=float(k),
                                              lot_size=self.lot_size, tick_size=0.05))
        self.store.add(Instrument(exchange=Exchange.BSE, token=SENSEX_TOKEN, tradingsymbol="SENSEX",
                                  underlying="SENSEX", instrument_type=InstrumentType.INDEX))
        tok = 80000
        for exp in self.expiries:
            for k in range(78000, 80001, 100):
                for t in (InstrumentType.CE, InstrumentType.PE):
                    tok += 1
                    self.store.add(Instrument(exchange=Exchange.BFO, token=str(tok),
                                              tradingsymbol=f"SENSEX{exp:%d%b%y}{k}{t.value}".upper(),
                                              underlying="SENSEX", instrument_type=t, expiry=exp, strike=float(k),
                                              lot_size=20, tick_size=0.05))

    async def login(self) -> Profile:
        self.logins += 1
        if self.fail_login:
            from brokers.base import BrokerError

            raise BrokerError("mock login failure", retryable=False)
        self._connected = True
        self.last_login_at = self.clock.now()
        return Profile(client_id="MOCK1", name="Mock", broker="mock")

    async def load_instruments(self, force: bool = False) -> int:
        return len(self.store)

    def resolve_symbol(self, key):  # type: ignore[override]
        inst = self.store.resolve(key)
        self._instruments[inst.key] = inst
        return inst

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        return self.store.option_expiries(underlying, exchange)

    def find_instrument(self, exchange: Exchange, token: str) -> Instrument | None:
        return self.store.by_token(exchange, token)

    def push(self, token: str, price: float, exchange: Exchange | None = None) -> None:
        if exchange is None:
            exchange = next(e for e in (Exchange.NSE, Exchange.BSE, Exchange.NFO, Exchange.BFO)
                            if self.store.by_token(e, token) is not None)
        inst = self.store.by_token(exchange, token)
        assert inst is not None, token
        self._last[inst.key] = price
        tick = Tick(exchange=inst.exchange, token=token, ltp=price, ts=self.clock.now())
        self._match_working(inst.exchange, token, price)
        self._emit_tick(tick)

    def option(self, strike: float, typ: str, expiry: date, underlying: str = "NIFTY") -> Instrument:
        for inst in self.store.all():
            if (inst.strike == strike and inst.instrument_type.value == typ and inst.expiry == expiry
                    and inst.underlying == underlying):
                return inst
        raise KeyError((strike, typ, expiry))

    async def get_historical_candles(self, instrument: Instrument, timeframe_min: int, start: datetime,
                                     end: datetime) -> list[Candle]:
        return [c for t, c in sorted(self.history.items()) if start <= t <= end and c.timeframe_min == timeframe_min]
