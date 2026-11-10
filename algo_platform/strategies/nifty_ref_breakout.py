"""Index 5-minute reference candle breakout (ATM option buying).

Works for NSE indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY — options on NFO) and BSE indices
(SENSEX, BANKEX — options on BFO); pick the exchange with the `exchange` setting.

At fixed times a specific index spot candle becomes the reference candle. After it closes:
  spot > ref_high + buffer  ->  BUY ATM CE   (spot SL: exit if spot <= ref_low)
  spot < ref_low  - buffer  ->  BUY ATM PE   (spot SL: exit if spot >= ref_high)
Entry is allowed from the reference close until the hard exit (reference START + exit_offset_min),
limited further by entry_window_candles. First breakout only, max one trade per slot, no
re-entry, one open position per instance. Every trade is force-closed at the slot's exit time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, InstrumentType, OrderType, Tick
from core.clock import combine, fmt_hm, hm_or_hms
from core.events import Category, Level
from strategies.base import (
    Direction,
    EntrySignal,
    ExitPlan,
    PartialTarget,
    Strategy,
    StrategySettings,
    TakeProfitMode,
    TradeInfo,
)
from strategies.registry import register

DEFAULT_REF_STARTS = ["09:45", "10:00", "10:15", "10:45", "11:15", "11:45", "12:10", "12:45",
                      "13:15", "13:45", "14:10", "14:35", "14:55"]
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
BSE_INDICES = {"SENSEX", "BANKEX", "SENSEX50"}
DERIVATIVE_EXCHANGE = {"NSE": Exchange.NFO, "BSE": Exchange.BFO}
# candle_close mode: a finished candle reaches the strategy a moment after it closes (the live candle
# builder seals it ~1.5 s late); keep the window open this long past the last candle's close
CANDLE_CLOSE_GRACE = timedelta(seconds=15)
# settings that no longer exist; dropped when old saved settings are loaded
RETIRED_KEYS = {"trailing_stop", "trail_timeframe_min", "skip_if_other_strategy_holds"}


class NiftyRefBreakoutSettings(StrategySettings):
    underlying: str = Field("NIFTY", title="Underlying index",
                            description="Index name as in the broker's instrument list: NIFTY, BANKNIFTY, FINNIFTY, "
                                        "MIDCPNIFTY (NSE) or SENSEX, BANKEX (BSE)")
    exchange: Literal["NSE", "BSE"] = Field(
        "NSE", title="Index exchange",
        description="NSE: index on NSE, options on NFO · BSE: index on BSE, options on BFO (SENSEX, BANKEX)")
    reference_candle_starts: list[time] = Field(
        default_factory=lambda: [time.fromisoformat(t) for t in DEFAULT_REF_STARTS],
        title="Reference candle start times (IST)",
        description="Fixed schedule of reference candles, comma separated HH:MM",
    )
    candle_timeframe_min: int = Field(5, ge=1, le=60, title="Candle timeframe (minutes)",
                                      description="Length of the reference candle")
    buffer_points: float = Field(2.25, ge=0, le=500, title="Buffer (points)",
                                 description="Added to ref high / subtracted from ref low to get the triggers")
    entry_window_candles: int = Field(2, ge=1, le=24, title="Entry window (candles)",
                                      description="Entries allowed from reference close for this many candles")
    exit_offset_min: int = Field(15, ge=1, le=375, title="Hard exit offset (minutes)",
                                 description="Hard exit = reference START + this many minutes")
    exit_early_sec: int = Field(2, ge=0, le=60, title="Exit early by (seconds)",
                                description="Timed exit this many seconds before reference start + offset, e.g. "
                                            "09:30 slot -> 09:44:58. The entry window closes at the same moment")
    lots: int = Field(5, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")
    strike_step: int = Field(50, ge=1, le=1000, title="Strike step",
                             description="ATM strike = spot rounded to the nearest multiple of this")
    expiry: Literal["nearest_weekly", "next_weekly"] = Field(
        "nearest_weekly", title="Expiry", description="nearest_weekly = nearest non-expired weekly contract")
    entry_trigger: Literal["tick", "candle_close"] = Field(
        "tick", title="Entry trigger", description="tick: first spot tick beyond the trigger; "
                                                    "candle_close: a candle must close beyond it")
    entry_order_type: Literal["MARKET", "LIMIT"] = Field("MARKET", title="Entry order type")
    limit_slippage_pct: float = Field(1.0, ge=0, le=20, title="LIMIT slippage (%)",
                                      description="LIMIT entry price = premium LTP × (1 + this %)")
    protective_sl_pct: float = Field(0.0, ge=0, le=95, title="Protective SL-M (% of premium)",
                                     description="Optional disaster stop at the broker on the option premium. Not part "
                                                 "of the strategy rules (the stop is on spot); 0 = off (default). "
                                                 "Set a wide value such as 70 if you want crash protection only")
    take_profit_mode: TakeProfitMode = Field(TakeProfitMode.NONE, title="Take-profit mode")
    take_profit_value: float = Field(0.0, ge=0, le=100000, title="Take-profit value",
                                     description="Points (spot_points), R multiple (r_multiple) or % (premium_pct)")
    candle_finalization_delay_sec: float = Field(5.0, ge=0, le=120, title="Candle finalization delay (s)",
                                                 description="Wait after the reference candle closes before reading it")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired(cls, v: Any) -> Any:
        # settings saved before the trailing stop was removed (06-Nov-2026) still load
        if isinstance(v, dict) and RETIRED_KEYS & v.keys():
            v = {k: x for k, x in v.items() if k not in RETIRED_KEYS}
        return v

    @field_validator("underlying")
    @classmethod
    def _norm_underlying(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("underlying is required")
        return v

    @field_validator("reference_candle_starts", mode="before")
    @classmethod
    def _parse_times(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [p for p in (x.strip() for x in v.replace(";", ",").replace("\n", ",").split(",")) if p]
        return v

    @field_validator("reference_candle_starts")
    @classmethod
    def _check_times(cls, v: list[time]) -> list[time]:
        if not v:
            raise ValueError("at least one reference candle start time is required")
        if len(set(v)) != len(v):
            raise ValueError("reference candle start times must be unique")
        for t in v:
            if t < MARKET_OPEN or t >= MARKET_CLOSE:
                raise ValueError(f"{fmt_hm(t)} is outside market hours 09:15-15:30")
            if t.second or t.microsecond:
                raise ValueError(f"{t} must be a whole minute")
        return sorted(v)

    @field_serializer("reference_candle_starts")
    def _ser_times(self, v: list[time]) -> list[str]:
        return [fmt_hm(t) for t in v]

    @model_validator(mode="after")
    def _cross_checks(self) -> "NiftyRefBreakoutSettings":
        tf = self.candle_timeframe_min
        for t in self.reference_candle_starts:
            if (t.hour * 60 + t.minute) % tf:
                raise ValueError(f"reference start {fmt_hm(t)} is not aligned to the {tf}-minute timeframe")
            exit_dt = datetime.combine(date.today(), t) + timedelta(minutes=self.exit_offset_min,
                                                                    seconds=-self.exit_early_sec)
            if exit_dt.time() > MARKET_CLOSE or exit_dt.date() != date.today():
                raise ValueError(f"slot {fmt_hm(t)}: exit at {exit_dt:%H:%M} is after market close")
        if self.exit_offset_min * 60 - self.exit_early_sec <= tf * 60:
            raise ValueError("exit_offset_min must be greater than the candle timeframe "
                             "(the exit must come after the reference candle closes)")
        if self.entry_trigger == "candle_close":
            # every candle in the entry window must close before the hard exit, or it can never trigger
            last_close = tf * (1 + self.entry_window_candles)
            if last_close >= self.exit_offset_min:
                usable = max(0, -(-self.exit_offset_min // tf) - 2)
                hint = f"set Entry window to {usable} or " if usable else ""
                raise ValueError(
                    f"candle_close entry: candle {self.entry_window_candles} of the entry window closes at "
                    f"reference start + {last_close} min, which is not before the hard exit (start + "
                    f"{self.exit_offset_min} min), so it could never trigger an entry. To fix it, {hint}set "
                    f"Hard exit offset to at least {last_close + tf} min")
        if self.underlying in BSE_INDICES and self.exchange != "BSE":
            raise ValueError(f"{self.underlying} trades on BSE: set Index exchange to BSE")
        if self.exchange == "BSE" and self.underlying not in BSE_INDICES:
            raise ValueError(f"{self.underlying} is not a BSE index: set Index exchange to NSE")
        if self.take_profit_mode is not TakeProfitMode.NONE and self.take_profit_value <= 0:
            raise ValueError("take_profit_value must be > 0 when a take-profit mode is selected")
        return self


class SlotState(str, Enum):
    PENDING = "PENDING"
    AWAITING_CANDLE = "AWAITING_CANDLE"
    WATCHING = "WATCHING"
    ENTERING = "ENTERING"
    IN_TRADE = "IN_TRADE"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    MISSED = "MISSED"


ACTIVE_STATES = {SlotState.AWAITING_CANDLE, SlotState.WATCHING, SlotState.ENTERING, SlotState.IN_TRADE}


@dataclass
class Slot:
    no: int
    day: date
    ref_start: datetime
    ref_end: datetime
    entry_end: datetime
    exit_at: datetime
    state: SlotState = SlotState.PENDING
    ref: Candle | None = None
    long_trigger: float | None = None
    short_trigger: float | None = None
    trade_id: str | None = None
    direction: Direction | None = None
    result: str | None = None
    events: list[str] = field(default_factory=list)
    ref_lookup_done: bool = False  # missed slot: tried to fetch its reference candle for display

    @property
    def slot_id(self) -> str:
        return f"{self.day.isoformat()}#{self.no}"

    @property
    def entry_deadline(self) -> datetime:
        return min(self.entry_end, self.exit_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "no": self.no,
            "slot_id": self.slot_id,
            "ref_start": fmt_hm(self.ref_start),
            "ref_end": fmt_hm(self.ref_end),
            "entry_end": hm_or_hms(self.entry_deadline),
            "exit_at": hm_or_hms(self.exit_at),
            "state": self.state.value,
            "ref_high": self.ref.high if self.ref else None,
            "ref_low": self.ref.low if self.ref else None,
            "long_trigger": self.long_trigger,
            "short_trigger": self.short_trigger,
            "direction": self.direction.value if self.direction else None,
            "trade_id": self.trade_id,
            "result": self.result,
        }


    def to_state(self) -> dict[str, Any]:
        """What is saved to the database so a restart can rebuild the slot (see NiftyRefBreakout._persist)."""
        return {
            "state": self.state.value, "result": self.result, "trade_id": self.trade_id,
            "direction": self.direction.value if self.direction else None,
            "ref": ({"open": self.ref.open, "high": self.ref.high, "low": self.ref.low, "close": self.ref.close,
                     "source": self.ref.source} if self.ref else None),
            "long_trigger": self.long_trigger, "short_trigger": self.short_trigger,
        }


# ------------------------------------------------------------------ pure rule helpers (unit tested)
def build_slots(settings: NiftyRefBreakoutSettings, day: date) -> list[Slot]:
    tf = timedelta(minutes=settings.candle_timeframe_min)
    slots = []
    for i, t in enumerate(settings.reference_candle_starts, start=1):
        start = combine(day, t)
        end = start + tf
        slots.append(Slot(no=i, day=day, ref_start=start, ref_end=end,
                          entry_end=end + tf * settings.entry_window_candles,
                          exit_at=start + timedelta(minutes=settings.exit_offset_min,
                                                    seconds=-settings.exit_early_sec)))
    return slots


def triggers(ref_high: float, ref_low: float, buffer_points: float) -> tuple[float, float]:
    return round(ref_high + buffer_points, 2), round(ref_low - buffer_points, 2)


def breakout_direction(spot: float, long_trigger: float, short_trigger: float) -> Direction | None:
    if spot > long_trigger:
        return Direction.CE_BUY
    if spot < short_trigger:
        return Direction.PE_BUY
    return None


def atm_strike(spot: float, step: int) -> float:
    """Spot rounded to the nearest strike step (halves round up)."""
    return float(math.floor(spot / step + 0.5) * step)


def spot_stop(direction: Direction, ref_high: float, ref_low: float) -> tuple[float, str]:
    """CE exits if spot <= ref_low; PE exits if spot >= ref_high."""
    if direction is Direction.CE_BUY:
        return ref_low, "below"
    return ref_high, "above"


def spot_sl_hit(direction: Direction, spot: float, ref_high: float, ref_low: float) -> bool:
    level, side = spot_stop(direction, ref_high, ref_low)
    return spot <= level if side == "below" else spot >= level


def take_profit_level(settings: NiftyRefBreakoutSettings, direction: Direction, entry_spot: float,
                      sl_level: float) -> tuple[float | None, str, float | None]:
    """Returns (tp_spot_level, tp_spot_side, tp_premium_pct)."""
    mode, v = settings.take_profit_mode, settings.take_profit_value
    side = "above" if direction is Direction.CE_BUY else "below"
    sign = 1 if direction is Direction.CE_BUY else -1
    if mode is TakeProfitMode.SPOT_POINTS:
        return round(entry_spot + sign * v, 2), side, None
    if mode is TakeProfitMode.R_MULTIPLE:
        risk = abs(entry_spot - sl_level)
        return round(entry_spot + sign * v * risk, 2), side, None
    if mode is TakeProfitMode.PREMIUM_PCT:
        return None, side, v
    return None, side, None


def pick_expiry(expiries: list[date], today: date, rule: str) -> date | None:
    live = sorted(e for e in expiries if e >= today)
    if not live:
        return None
    if rule == "next_weekly" and len(live) > 1:
        return live[1]
    return live[0]


# ------------------------------------------------------------------ strategy
@register
class NiftyRefBreakout(Strategy):
    template_id: ClassVar[str] = "nifty_ref_breakout"
    display_name: ClassVar[str] = "Index 5-min Reference Candle Breakout (ATM option buying)"
    description: ClassVar[str] = ("Fixed-schedule reference candles; buy ATM CE above high+buffer or ATM PE below "
                                  "low−buffer; spot-based SL; hard exit at reference start + offset.")
    version: ClassVar[str] = "2.0"
    settings_model: ClassVar[type[StrategySettings]] = NiftyRefBreakoutSettings

    def __init__(self, settings: NiftyRefBreakoutSettings, ctx: Any) -> None:
        super().__init__(settings, ctx)
        self.settings: NiftyRefBreakoutSettings = settings
        self.day: date | None = None
        self.slots: list[Slot] = []
        self.spot: Instrument | None = None
        self._restored: dict[str, TradeInfo] = {}
        self._missed_reported: date | None = None
        self._saved_sig: Any = None

    # ---- helpers
    @property
    def spot_key(self) -> InstrumentKey:
        return InstrumentKey(underlying=self.settings.underlying, exchange=Exchange(self.settings.exchange),
                             instrument_type=InstrumentType.INDEX)

    @property
    def option_exchange(self) -> Exchange:
        return DERIVATIVE_EXCHANGE[self.settings.exchange]

    def subscriptions(self) -> list[InstrumentKey]:
        return [self.spot_key]

    def candle_timeframes(self) -> list[int]:
        return [self.settings.candle_timeframe_min]

    def _emit(self, event_type: str, message: str, level: Level = Level.INFO, **payload: Any) -> None:
        self.ctx.emit(event_type, message, level, Category.STRATEGY, **payload)

    def _load_saved(self, day: date) -> dict[str, dict[str, Any]]:
        try:
            return dict((self.ctx.load_day_state(day) or {}).get("slots") or {})
        except Exception:
            return {}

    def _persist(self) -> None:
        """Save today's slots (reference candle, triggers, state) whenever they change, so a restart keeps
        them and a slot that already signalled is never traded again."""
        if self.day is None:
            return
        data = {fmt_hm(s.ref_start): s.to_state() for s in self.slots
                if s.state is not SlotState.PENDING or s.ref is not None}
        if data == self._saved_sig:
            return
        try:
            self.ctx.save_day_state(self.day, {"slots": data})
            self._saved_sig = data
        except Exception as exc:
            self._emit("SLOT_STATE_SAVE_FAILED", f"could not save slot state: {exc}", Level.WARNING)

    def _build_day(self, day: date, now: datetime) -> None:
        self.day = day
        self.slots = build_slots(self.settings, day)
        saved = self._load_saved(day)
        missed = []
        for s in self.slots:
            sv = saved.get(fmt_hm(s.ref_start))
            if sv and sv.get("ref"):
                r = sv["ref"]
                s.ref = Candle(start=s.ref_start, timeframe_min=self.settings.candle_timeframe_min, open=r["open"],
                               high=r["high"], low=r["low"], close=r["close"], source=r.get("source") or "saved")
                s.long_trigger, s.short_trigger = sv.get("long_trigger"), sv.get("short_trigger")
                if s.long_trigger is None or s.short_trigger is None:
                    s.long_trigger, s.short_trigger = triggers(s.ref.high, s.ref.low, self.settings.buffer_points)
            if s.slot_id in self._restored:
                info = self._restored[s.slot_id]
                s.trade_id, s.direction = info.trade_id, Direction(info.direction)
                if info.status == "OPEN":
                    s.state = SlotState.IN_TRADE
                else:
                    s.state, s.result = SlotState.DONE, info.exit_reason or info.status.lower()
            elif sv and sv.get("state") in (SlotState.DONE.value, SlotState.SKIPPED.value, SlotState.MISSED.value,
                                            SlotState.ENTERING.value, SlotState.IN_TRADE.value):
                # already decided before the restart: never trade this slot again
                s.state = SlotState.MISSED if sv["state"] == SlotState.MISSED.value else (
                    SlotState.DONE if sv["state"] == SlotState.DONE.value else SlotState.SKIPPED)
                s.result = sv.get("result") or ("interrupted_entry" if sv["state"] == SlotState.ENTERING.value
                                                else "interrupted")
                s.direction = Direction(sv["direction"]) if sv.get("direction") else None
                s.trade_id = sv.get("trade_id")
                s.ref_lookup_done = True
            elif now >= s.exit_at:
                s.state, s.result = SlotState.MISSED, "started_after_slot"
                missed.append(s.no)
            elif s.ref is not None and now >= s.ref_end:
                s.state = SlotState.WATCHING  # resume the entry window with the saved reference candle
        if missed and self._missed_reported != day:
            self._missed_reported = day
            self._emit("SLOTS_MISSED", f"slots {missed} already finished before start", Level.DEBUG)

    # ---- lifecycle
    async def on_start(self) -> None:
        self.spot = self.ctx.resolve(self.spot_key)
        now = self.ctx.now()
        self._build_day(now.date(), now)

    def restore(self, trades: list[TradeInfo]) -> None:
        self._restored = {t.slot_id: t for t in trades}
        if self.day is not None:
            self._build_day(self.day, self.ctx.now())

    async def on_timer(self, now: datetime) -> None:
        if self.day != now.date():
            self._restored = {}
            self._build_day(now.date(), now)
        delay = timedelta(seconds=self.settings.candle_finalization_delay_sec)
        for s in self.slots:
            if s.state is SlotState.PENDING and now >= s.ref_end + delay:
                s.state = SlotState.AWAITING_CANDLE
            if s.state is SlotState.AWAITING_CANDLE:
                await self._load_reference(s, now)
            if s.state is SlotState.WATCHING and now >= self._window_close_at(s):
                self._close_window(s, "no_breakout")
        await self._fill_missed_reference()
        self.maybe_apply_pending()
        self._persist()

    async def _fill_missed_reference(self) -> None:
        """Show the reference candle of a slot that finished while the app was off (one per timer tick, so
        a restart does not burst the broker's historical API). Display only: such a slot never trades."""
        s = next((x for x in self.slots if x.state is SlotState.MISSED and x.ref is None and not x.ref_lookup_done),
                 None)
        if s is None or self.spot is None:
            return
        s.ref_lookup_done = True
        try:
            candle = await self.ctx.get_candle(self.spot, s.ref_start, self.settings.candle_timeframe_min)
        except Exception:
            candle = None
        if candle is not None:
            s.ref = candle
            s.long_trigger, s.short_trigger = triggers(candle.high, candle.low, self.settings.buffer_points)
            s.result = "app_was_off"

    async def _load_reference(self, s: Slot, now: datetime) -> None:
        assert self.spot is not None
        if now >= s.entry_deadline:
            s.state, s.result = SlotState.SKIPPED, "no_reference_candle"
            self._emit("SLOT_SKIPPED", f"SKIP slot=#{s.no} reason=no_reference_candle", Level.WARNING,
                       slot=s.no, reason="no_reference_candle")
            return
        candle = await self.ctx.get_candle(self.spot, s.ref_start, self.settings.candle_timeframe_min)
        if candle is None:
            return  # retried on the next timer tick
        s.ref = candle
        s.long_trigger, s.short_trigger = triggers(candle.high, candle.low, self.settings.buffer_points)
        s.state = SlotState.WATCHING
        self._emit(
            "REF_CANDLE",
            f"REF slot=#{s.no} candle={fmt_hm(s.ref_start)} high={candle.high:.2f} low={candle.low:.2f} "
            f"long_trigger={s.long_trigger:.2f} short_trigger={s.short_trigger:.2f} exit={hm_or_hms(s.exit_at)} "
            f"O={candle.open:.2f} C={candle.close:.2f}",
            slot=s.no, candle_start=s.ref_start, candle_end=s.ref_end, open=candle.open, high=candle.high,
            low=candle.low, close=candle.close, long_trigger=s.long_trigger, short_trigger=s.short_trigger,
            buffer=self.settings.buffer_points, exit=s.exit_at, source=candle.source,
        )
        self._emit("ENTRY_WINDOW_OPENED",
                   f"slot=#{s.no} window {fmt_hm(s.ref_end)}-{hm_or_hms(s.entry_deadline)}", Level.DEBUG,
                   slot=s.no, window_start=s.ref_end, window_end=s.entry_deadline)
        if self.settings.entry_trigger == "tick":
            ltp = self.ctx.ltp(self.spot)
            if ltp is not None:
                await self._evaluate(s, ltp, now)

    def _window_close_at(self, s: Slot) -> datetime:
        if self.settings.entry_trigger == "candle_close":
            return min(s.entry_end + CANDLE_CLOSE_GRACE, s.exit_at)
        return s.entry_deadline

    def _close_window(self, s: Slot, reason: str) -> None:
        s.state, s.result = SlotState.SKIPPED, reason
        self._emit("ENTRY_WINDOW_CLOSED", f"slot=#{s.no} window closed", Level.DEBUG, slot=s.no)
        self._emit("SLOT_SKIPPED", f"SKIP slot=#{s.no} reason={reason}", slot=s.no, reason=reason)

    async def on_tick(self, tick: Tick) -> None:
        if self.spot is None or tick.token != self.spot.token or self.settings.entry_trigger != "tick":
            return
        now = self.ctx.now()
        for s in self.slots:
            if s.state is SlotState.WATCHING:
                await self._evaluate(s, tick.ltp, now)
        self._persist()

    async def on_candle(self, instrument: Instrument, candle: Candle) -> None:
        if (self.spot is None or instrument.token != self.spot.token or self.settings.entry_trigger != "candle_close"
                or candle.timeframe_min != self.settings.candle_timeframe_min):
            return
        close_time = candle.start + timedelta(minutes=candle.timeframe_min)
        for s in self.slots:
            if s.state is not SlotState.WATCHING or candle.start < s.ref_end:
                continue
            if close_time > s.entry_end or close_time >= s.exit_at:
                continue
            spot_now = self.ctx.ltp(self.spot) or candle.close
            await self._evaluate(s, candle.close, self.ctx.now(), spot_for_strike=spot_now, candle_close=close_time)
        self._persist()

    async def _evaluate(self, s: Slot, price: float, now: datetime, spot_for_strike: float | None = None,
                        candle_close: datetime | None = None) -> None:
        if s.state is not SlotState.WATCHING or s.long_trigger is None or s.short_trigger is None or s.ref is None:
            return
        if candle_close is not None:
            # judged by when the candle closed (it arrives a moment later); never at/after the hard exit
            if candle_close <= s.ref_end or candle_close > s.entry_end or now >= s.exit_at:
                return
        elif now < s.ref_end or now >= s.entry_deadline:
            return
        direction = breakout_direction(price, s.long_trigger, s.short_trigger)
        if direction is None:
            return
        # First breakout only: whatever happens next, this slot does not trade again.
        s.state, s.direction = SlotState.ENTERING, direction
        if self.spot is None or (self.spot.underlying or "").upper() != self.settings.underlying:
            # never trade one index's options off another index's prices
            s.state, s.result = SlotState.SKIPPED, "spot_mismatch"
            self._emit("SIGNAL_REJECTED", f"slot=#{s.no} reason=spot_mismatch watching "
                       f"{self.spot.underlying if self.spot else '-'} but configured for {self.settings.underlying}",
                       Level.ERROR, slot=s.no, reason="spot_mismatch")
            return
        trigger = s.long_trigger if direction is Direction.CE_BUY else s.short_trigger
        if self.ctx.has_open_position():
            s.state, s.result = SlotState.SKIPPED, "already_in_position"
            self._emit("SIGNAL_REJECTED", f"slot=#{s.no} dir={direction.value} reason=already_in_position",
                       Level.WARNING, slot=s.no, reason="already_in_position")
            return
        spot = spot_for_strike if spot_for_strike is not None else price
        strike = atm_strike(spot, self.settings.strike_step)
        expiry = pick_expiry(self.ctx.option_expiries(self.settings.underlying, self.option_exchange), now.date(),
                             self.settings.expiry)
        if expiry is None:
            s.state, s.result = SlotState.SKIPPED, "no_expiry_available"
            self._emit("SIGNAL_REJECTED", f"slot=#{s.no} reason=no_expiry_available", Level.ERROR, slot=s.no)
            return
        sl_level, sl_side = spot_stop(direction, s.ref.high, s.ref.low)
        tp_level, tp_side, tp_pct = take_profit_level(self.settings, direction, spot, sl_level)
        signal = EntrySignal(
            slot_id=s.slot_id,
            direction=direction,
            option=InstrumentKey(underlying=self.settings.underlying, exchange=self.option_exchange,
                                 instrument_type=InstrumentType.CE if direction is Direction.CE_BUY else InstrumentType.PE,
                                 expiry=expiry, strike=strike),
            lots=self.settings.lots,
            spot=spot,
            trigger=trigger,
            order_type=OrderType(self.settings.entry_order_type),
            limit_slippage_pct=self.settings.limit_slippage_pct,
            signal_time=now,
            exit_plan=ExitPlan(
                exit_at=s.exit_at, underlying=self.spot, spot_sl=sl_level, spot_sl_side=sl_side,
                tp_spot_level=tp_level, tp_spot_side=tp_side, tp_premium_pct=tp_pct,
                protective_sl_pct=self.settings.protective_sl_pct or None,
                partial_targets=self.partial_targets(direction, spot, s.ref),
            ),
            meta={"slot_no": s.no, "underlying": self.settings.underlying, "ref_start": fmt_hm(s.ref_start), "ref_high": s.ref.high, "ref_low": s.ref.low,
                  "buffer": self.settings.buffer_points, "trigger_price": price},
        )
        self._persist()  # saved as ENTERING before the order: a restart now can never enter this slot again
        info = await self.ctx.submit_entry(signal)
        if info is None:
            s.state, s.result = SlotState.SKIPPED, "signal_rejected"
            return
        s.trade_id = info.trade_id
        if info.status == "OPEN":
            s.state = SlotState.IN_TRADE
        elif info.status == "CLOSED":
            s.state, s.result = SlotState.DONE, info.exit_reason
        else:
            s.state, s.result = SlotState.DONE, info.exit_reason or "entry_failed"
        self._persist()

    def partial_targets(self, direction: Direction, entry_spot: float, ref: Candle) -> list[PartialTarget]:
        """Scale-out levels (TP1, TP2, ...) for a new trade. None by default; TP variants override this."""
        return []

    async def on_trade_update(self, trade: TradeInfo) -> None:
        for s in self.slots:
            if s.slot_id == trade.slot_id:
                s.trade_id = trade.trade_id
                if trade.status == "OPEN":
                    s.state = SlotState.IN_TRADE
                else:
                    # closed for any reason (SL, TP, timed exit, manual exit at broker, kill switch):
                    # the slot is finished; there is no re-entry.
                    s.state, s.result = SlotState.DONE, trade.exit_reason or trade.status.lower()
        self.maybe_apply_pending()
        self._persist()

    # ---- settings
    def is_idle(self) -> bool:
        return not any(s.state in ACTIVE_STATES for s in self.slots)

    def apply_settings(self, new: StrategySettings) -> None:
        assert isinstance(new, NiftyRefBreakoutSettings)
        self.settings = new
        if self.day is None:
            return
        now = self.ctx.now()
        delay = timedelta(seconds=new.candle_finalization_delay_sec)
        keep = [s for s in self.slots if s.state is not SlotState.PENDING]
        fresh = [s for s in build_slots(new, self.day) if now < s.ref_end + delay]
        kept_ids = {(s.ref_start) for s in keep}
        fresh = [s for s in fresh if s.ref_start not in kept_ids]
        merged = sorted(keep + fresh, key=lambda s: s.ref_start)
        # renumber pending slots so numbering follows the new schedule
        schedule = {combine(self.day, t): i for i, t in enumerate(new.reference_candle_starts, start=1)}
        for s in merged:
            if s.state is SlotState.PENDING and s.ref_start in schedule:
                s.no = schedule[s.ref_start]
        self.slots = merged

    # ---- UI
    def phase(self) -> str:
        states = {s.state for s in self.slots}
        if states & {SlotState.IN_TRADE, SlotState.ENTERING}:
            return "IN_TRADE"
        if states & {SlotState.WATCHING, SlotState.AWAITING_CANDLE}:
            return "WATCHING"
        return "WAITING"

    def status(self) -> dict[str, Any]:
        nxt = next((s for s in self.slots if s.state is SlotState.PENDING), None)
        return {
            "day": self.day.isoformat() if self.day else None,
            "slots": [s.to_dict() for s in self.slots],
            "next_slot": nxt.to_dict() if nxt else None,
            "pending_settings": self.has_pending_settings,
        }
