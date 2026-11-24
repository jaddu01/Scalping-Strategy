"""Strategy plugin interface.

A strategy template is a class that:
- declares its settings as a Pydantic model (`settings_model`); the UI form is generated from it
- receives market events (ticks, closed candles, a ~1s timer) through async handlers
- asks the engine to open positions by calling `ctx.submit_entry(EntrySignal)`

Strategies never talk to brokers directly. The engine resolves symbols, runs risk checks,
places and confirms orders, and manages exits from the ExitPlan attached to each signal. The
same strategy code runs live, in paper mode, and inside the backtester.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, OrderType, Tick
from core.events import Category, Level


class StrategySettings(BaseModel):
    """Base class for strategy settings. Every field needs a default, a description and bounds."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Direction(str, Enum):
    CE_BUY = "CE_BUY"
    PE_BUY = "PE_BUY"


class TakeProfitMode(str, Enum):
    NONE = "none"
    SPOT_POINTS = "spot_points"
    R_MULTIPLE = "r_multiple"
    PREMIUM_PCT = "premium_pct"


@dataclass
class PartialTarget:
    """Sell `lots` of the position when spot reaches `spot_level` (e.g. TP1, TP2). The rest stays open."""

    label: str  # "tp1", "tp2" — also the exit reason if this target closes the last lots
    spot_level: float | None
    side: str  # "above": hit when spot >= level (CE); "below": hit when spot <= level (PE)
    lots: int
    # premium target instead of a spot level: hit when the option's price >= entry fill × (1 + pct/100).
    # The level is set by the engine once the entry fill price is known.
    premium_pct: float | None = None
    # premium % chosen by the entry fill price: ((upper bound, pct), ...) ascending; the first band whose bound is
    # above the fill applies (bounds: "< 80" -> 80, "80-150" -> 150 inclusive, "above" -> inf). Engine resolves it.
    premium_bands: tuple[tuple[float, float], ...] | None = None


@dataclass
class ExitPlan:
    """Everything the exit manager needs to close a position without asking the strategy."""

    exit_at: datetime
    underlying: Instrument | None = None
    spot_sl: float | None = None
    spot_sl_side: str = "below"  # "below": exit when spot <= spot_sl; "above": exit when spot >= spot_sl
    tp_spot_level: float | None = None
    tp_spot_side: str = "above"  # "above": exit when spot >= level; "below": exit when spot <= level
    tp_premium_pct: float | None = None  # exit when premium >= fill * (1 + pct/100)
    protective_sl_pct: float | None = None  # broker-side disaster SL on premium
    partial_targets: list[PartialTarget] = field(default_factory=list)  # scale-out levels, in order
    # runner protection after the start TP fills: {"start": "tp2", "step": "one_behind", "band1_max": 80,
    # "band2_max": 150, "trail_pcts": [20, 15, 10]}; None = off. The engine picks the trail % from the entry fill
    runner: dict[str, Any] | None = None


@dataclass
class EntrySignal:
    slot_id: str
    direction: Direction
    option: InstrumentKey
    lots: int
    spot: float
    trigger: float
    exit_plan: ExitPlan
    order_type: OrderType = OrderType.MARKET
    limit_slippage_pct: float = 0.0
    signal_time: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeInfo:
    """What the engine tells a strategy about its trades."""

    trade_id: str
    slot_id: str
    direction: str
    status: str  # OPEN | CLOSED | FAILED
    exit_reason: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    pnl: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class StrategyContext(Protocol):
    """Services the engine (or the backtester) provides to a running strategy."""

    instance_id: int | None
    instance_name: str

    def now(self) -> datetime: ...

    def emit(self, event_type: str, message: str, level: Level = Level.INFO,
             category: Category = Category.STRATEGY, trade_id: str | None = None, **payload: Any) -> None: ...

    def resolve(self, key: InstrumentKey) -> Instrument: ...

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]: ...

    def ltp(self, instrument: Instrument) -> float | None: ...

    async def get_candle(self, instrument: Instrument, start: datetime, timeframe_min: int) -> Candle | None: ...

    async def submit_entry(self, signal: EntrySignal) -> TradeInfo | None: ...

    def has_open_position(self) -> bool: ...

    def square_off_time(self) -> time: ...

    def is_trading_day(self, d: date) -> bool:
        """False on weekends and exchange holidays."""
        ...

    def load_day_state(self, day: date) -> dict[str, Any]:
        """Strategy state saved earlier today (empty if none), so a restart can rebuild the day."""
        ...

    def save_day_state(self, day: date, state: dict[str, Any]) -> None: ...

    async def history(self, instrument: Instrument, timeframe_min: int, count: int) -> list[Candle]:
        """The last `count` completed candles of `instrument` (oldest first), e.g. to warm up indicators."""
        ...

    async def recent_candles(self, instrument: Instrument, timeframe_min: int, count: int) -> list[Candle]:
        """Like `history`, but served from one in-memory buffer per index shared by all strategies (live engine
        only; strategies fall back to `history` when a context does not have it)."""
        ...

    def tighten_stop(self, trade_id: str, level: float) -> None:
        """Move the trade's spot stop-loss to `level` if that is tighter (trailing stop); never loosens it."""
        ...

    async def request_exit(self, trade_id: str, reason: str, partial: str | None = None) -> None:
        """Ask the engine to close the trade (or, with `partial`, sell that scale-out target's lots, e.g.
        "tp1"). Used for exits decided by the strategy itself, such as an RSI level on candle close."""
        ...


class Strategy(abc.ABC):
    template_id: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]]

    def __init__(self, settings: StrategySettings, ctx: StrategyContext) -> None:
        self.settings = settings
        self.ctx = ctx
        self._pending_settings: StrategySettings | None = None

    # ---- lifecycle
    @abc.abstractmethod
    def subscriptions(self) -> list[InstrumentKey]:
        """Instruments whose ticks this strategy needs (e.g. the NIFTY index)."""

    def candle_timeframes(self) -> list[int]:
        return []

    async def on_start(self) -> None:
        return None

    async def on_stop(self) -> None:
        return None

    async def on_tick(self, tick: Tick) -> None:
        return None

    async def on_candle(self, instrument: Instrument, candle: Candle) -> None:
        return None

    async def on_timer(self, now: datetime) -> None:
        return None

    async def on_trade_update(self, trade: TradeInfo) -> None:
        return None

    def restore(self, trades: list[TradeInfo]) -> None:
        """Rebuild today's state after a restart from the trades already stored."""

    # ---- settings
    def is_idle(self) -> bool:
        """True when no slot is active, so new settings can be applied without changing a live trade."""
        return True

    def request_settings_update(self, new: StrategySettings) -> bool:
        """Queue new settings; they are applied at the next idle moment (never mid-trade)."""
        self._pending_settings = new
        return self.maybe_apply_pending()

    def maybe_apply_pending(self) -> bool:
        if self._pending_settings is not None and self.is_idle():
            new, self._pending_settings = self._pending_settings, None
            self.apply_settings(new)
            self.ctx.emit("PARAMS_APPLIED", "parameters applied", params=new.model_dump(mode="json"))
            return True
        return False

    @property
    def has_pending_settings(self) -> bool:
        return self._pending_settings is not None

    def apply_settings(self, new: StrategySettings) -> None:
        self.settings = new

    def indicator_snapshot(self, price: float | None = None) -> dict[str, float] | None:
        """Indicator values at `price` (e.g. RSI / EMA) for trade reports; None if the strategy has none."""
        return None

    # ---- UI
    def status(self) -> dict[str, Any]:
        """Free-form state for the UI (e.g. today's slots)."""
        return {}

    def phase(self) -> str:
        """WAITING (between opportunities) | WATCHING (entry window open) | IN_TRADE."""
        return "WAITING"
