"""Broker adapter interface and the normalized models used everywhere in the platform.

Every adapter maps its broker's native payloads to these models and back. Nothing outside
`brokers/` should ever see a broker-specific field name.
"""
from __future__ import annotations

import abc
from datetime import date, datetime
from enum import Enum
from typing import Any, Awaitable, Callable, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from core.clock import now_ist


# --------------------------------------------------------------------------- enums
class Exchange(str, Enum):
    NSE = "NSE"
    NFO = "NFO"
    BSE = "BSE"
    BFO = "BFO"
    MCX = "MCX"
    CDS = "CDS"


class InstrumentType(str, Enum):
    INDEX = "INDEX"
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # stop-loss limit
    SL_M = "SL_M"  # stop-loss market


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"
    CARRYFORWARD = "CARRYFORWARD"
    MARGIN = "MARGIN"


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # sent, no broker id yet
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.COMPLETE, OrderStatus.REJECTED, OrderStatus.CANCELLED)

    @property
    def is_working(self) -> bool:
        return self in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PARTIALLY_FILLED,
                        OrderStatus.PENDING_SUBMIT)


# --------------------------------------------------------------------------- models
class _Model(BaseModel):
    model_config = ConfigDict(use_enum_values=False, frozen=False)


class InstrumentKey(_Model):
    """Broker-independent description of an instrument (what a strategy asks for)."""

    underlying: str  # e.g. "NIFTY"
    exchange: Exchange
    instrument_type: InstrumentType
    expiry: date | None = None
    strike: float | None = None

    def label(self) -> str:
        if self.instrument_type in (InstrumentType.CE, InstrumentType.PE):
            return f"{self.underlying} {self.expiry:%d%b%y} {self.strike:g} {self.instrument_type.value}".upper()
        return f"{self.underlying} {self.instrument_type.value}"


class Instrument(_Model):
    """A resolved, broker-specific tradable instrument."""

    exchange: Exchange
    token: str
    tradingsymbol: str
    underlying: str
    instrument_type: InstrumentType
    expiry: date | None = None
    strike: float | None = None
    lot_size: int = 1
    tick_size: float = 0.05

    @property
    def key(self) -> tuple[str, str]:
        return (self.exchange.value, self.token)


class OrderRequest(_Model):
    instrument: Instrument
    side: Side
    quantity: int  # units, not lots
    order_type: OrderType = OrderType.MARKET
    product: ProductType = ProductType.INTRADAY
    validity: Validity = Validity.DAY
    price: float | None = None
    trigger_price: float | None = None
    tag: str | None = None


class OrderModify(_Model):
    order_id: str
    instrument: Instrument
    order_type: OrderType
    quantity: int
    price: float | None = None
    trigger_price: float | None = None
    product: ProductType = ProductType.INTRADAY
    validity: Validity = Validity.DAY


class Order(_Model):
    order_id: str
    exchange: Exchange
    tradingsymbol: str
    token: str
    side: Side
    quantity: int
    filled_quantity: int = 0
    pending_quantity: int = 0
    average_price: float = 0.0
    price: float | None = None
    trigger_price: float | None = None
    order_type: OrderType = OrderType.MARKET
    product: ProductType = ProductType.INTRADAY
    validity: Validity = Validity.DAY
    status: OrderStatus = OrderStatus.UNKNOWN
    status_message: str = ""
    tag: str | None = None
    exchange_order_id: str | None = None
    placed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=now_ist)
    exchange_time: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class Fill(_Model):
    fill_id: str
    order_id: str
    exchange: Exchange
    tradingsymbol: str
    token: str
    side: Side
    price: float
    quantity: int
    time: datetime
    product: ProductType = ProductType.INTRADAY


class Position(_Model):
    exchange: Exchange
    tradingsymbol: str
    token: str
    product: ProductType
    net_quantity: int
    average_price: float = 0.0  # average of the open (net) quantity
    buy_quantity: int = 0
    sell_quantity: int = 0
    buy_average: float = 0.0
    sell_average: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    pnl: float = 0.0  # broker's total P&L for the line
    ltp: float = 0.0
    lot_size: int = 1


class Tick(_Model):
    exchange: Exchange
    token: str
    ltp: float
    ts: datetime = Field(default_factory=now_ist)
    volume: int | None = None


class Candle(_Model):
    start: datetime
    timeframe_min: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    complete: bool = True
    source: str = "ticks"  # "ticks" | "historical"


class Funds(_Model):
    available_cash: float = 0.0
    used_margin: float = 0.0
    net: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class Profile(_Model):
    client_id: str
    name: str = ""
    broker: str = ""
    email: str | None = None
    exchanges: list[str] = Field(default_factory=list)


class RateLimited(Exception):
    """Marker mixin: the broker refused the call for exceeding its rate limit (safe to retry shortly)."""


class BrokerError(Exception):
    """Raised by adapters for any broker-side failure. `retryable` drives backoff logic."""

    def __init__(self, message: str, *, retryable: bool = False, code: str | None = None,
                 response: Any = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.response = response



class BrokerRateLimited(BrokerError, RateLimited):
    """The broker refused the call for exceeding its rate limit."""

class OrderRejected(BrokerError):
    pass


class SessionExpired(BrokerError):
    def __init__(self, message: str = "session expired", **kw: Any):
        super().__init__(message, retryable=True, **kw)


TickCallback = Callable[[Tick], None]
OrderUpdateCallback = Callable[[Order], None]
ConnectionCallback = Callable[[str, str, str], None]  # (stream, state, detail)


class BrokerCredentials(BaseModel):
    """Base for each adapter's credential schema. Mark secret fields with
    `json_schema_extra={"secret": True}`; they are encrypted and only ever shown masked."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def secret_fields(cls) -> set[str]:
        out = set()
        for name, f in cls.model_fields.items():
            extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
            if extra.get("secret"):
                out.add(name)
        return out


class BrokerAdapter(abc.ABC):
    """Common interface implemented by every broker.

    All network methods are async. Synchronous SDKs must be wrapped with asyncio.to_thread
    so they never block the event loop. Ticks and order updates are delivered through the
    callbacks registered with subscribe_ticks()/on_order_update(); callbacks are always
    invoked on the event loop thread.
    """

    broker_type: ClassVar[str] = "base"
    display_name: ClassVar[str] = "Base"
    credentials_model: ClassVar[type[BrokerCredentials]] = BrokerCredentials
    supports_order_stream: ClassVar[bool] = False
    supports_tick_stream: ClassVar[bool] = False
    is_paper: ClassVar[bool] = False
    # Max length / allowed charset of the broker's order tag field.
    order_tag_max_len: ClassVar[int] = 20

    def __init__(self, credentials: BrokerCredentials, *, account_id: int | None = None,
                 label: str = "", cache_dir: Any = None) -> None:
        self.credentials = credentials
        self.account_id = account_id
        self.label = label
        self.cache_dir = cache_dir
        self._tick_callbacks: list[TickCallback] = []
        self._order_callbacks: list[OrderUpdateCallback] = []
        self._connection_callbacks: list[ConnectionCallback] = []
        self.last_login_at: datetime | None = None

    # ---- session
    @abc.abstractmethod
    async def login(self) -> Profile: ...

    @abc.abstractmethod
    async def refresh_session(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    async def logout(self) -> None:
        return None

    async def close(self) -> None:
        """Stop streams and release resources."""
        return None

    # ---- account
    @abc.abstractmethod
    async def get_profile(self) -> Profile: ...

    @abc.abstractmethod
    async def get_funds(self) -> Funds: ...

    # ---- instruments
    @abc.abstractmethod
    async def load_instruments(self, force: bool = False) -> int:
        """Download/refresh the instrument master. Returns number of instruments loaded."""

    @abc.abstractmethod
    def resolve_symbol(self, key: InstrumentKey) -> Instrument:
        """Map a normalized InstrumentKey to this broker's instrument (symbol + token)."""

    @abc.abstractmethod
    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]: ...

    @abc.abstractmethod
    def find_instrument(self, exchange: Exchange, token: str) -> Instrument | None: ...

    # ---- orders
    @abc.abstractmethod
    async def place_order(self, req: OrderRequest) -> str:
        """Place an order; returns the broker order id. Raises OrderRejected / BrokerError."""

    @abc.abstractmethod
    async def modify_order(self, req: OrderModify) -> str: ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> str: ...

    @abc.abstractmethod
    async def get_order_book(self) -> list[Order]: ...

    @abc.abstractmethod
    async def get_trade_book(self) -> list[Fill]: ...

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]: ...

    # ---- market data
    @abc.abstractmethod
    async def get_ltp(self, instruments: list[Instrument]) -> dict[tuple[str, str], float]: ...

    @abc.abstractmethod
    async def get_historical_candles(self, instrument: Instrument, timeframe_min: int,
                                     start: datetime, end: datetime) -> list[Candle]: ...

    @abc.abstractmethod
    async def subscribe_ticks(self, instruments: list[Instrument]) -> None: ...

    @abc.abstractmethod
    async def unsubscribe_ticks(self, instruments: list[Instrument]) -> None: ...

    # ---- callbacks
    def add_tick_listener(self, cb: TickCallback) -> None:
        self._tick_callbacks.append(cb)

    def remove_tick_listener(self, cb: TickCallback) -> None:
        if cb in self._tick_callbacks:
            self._tick_callbacks.remove(cb)

    def on_order_update(self, cb: OrderUpdateCallback) -> None:
        """Register a callback for live order status changes."""
        self._order_callbacks.append(cb)

    def on_connection_change(self, cb: ConnectionCallback) -> None:
        self._connection_callbacks.append(cb)

    def _emit_tick(self, tick: Tick) -> None:
        for cb in list(self._tick_callbacks):
            cb(tick)

    def _emit_order(self, order: Order) -> None:
        for cb in list(self._order_callbacks):
            cb(order)

    def _emit_connection(self, stream: str, state: str, detail: str = "") -> None:
        for cb in list(self._connection_callbacks):
            cb(stream, state, detail)


AsyncFn = Callable[..., Awaitable[Any]]
