"""NIFTY EMA21 + RSI50 Breakout — from "ema21_rsi50_candle_sl.pine" (TradingView), on this platform's rules.

Pine logic on 5-minute NIFTY spot candles:
  setup (long)   a candle closes above EMA 21 and RSI(14) crosses above 50, the two crosses within
                 `sync_bars` candles; the later cross candle is the CONFIRMATION candle (only when flat)
  entry          spot breaks the confirmation high + buffer (1 tick) within the next `valid_bars` candles
                 -> BUY the ATM CE (shorts mirror this: below the low - buffer -> BUY the ATM PE)
  setup void     no breakout in time, or a candle closes back below EMA 21
  stop           confirmation candle low - buffer (or the swing low), checked on every spot tick
Exits on this platform (TP1 / TP2 replace the script's single RSI 70 exit):
  TP1            sell 40 % of the lots as soon as the live RSI reaches 68     (PE: RSI <= 32)
  TP2            sell 40 % more when the live RSI reaches 80                  (PE: RSI <= 20)
                 live RSI = RSI with the current tick as the forming candle's close (TradingView's real-time
                 value), so a target fires mid-candle; set "RSI targets checked on" to candle_close for the
                 Pine behaviour
  trail          after TP1 the stop of the remaining lots trails 1.5 × ATR behind the best price since entry
  rest           spot SL / trailing stop, the script's early exit (a close back through EMA 21), or the 15:15 hard exit
New entries only between 09:20 and 15:00; one position at a time; risk checks as usual.
Sideways filter (core/regime.py): when a setup arms and again when its breakout fires, five chop checks run on the
completed candles (flat EMA, low ADX, high Choppiness, many EMA crosses, tight 1-hour box). "log_only" (default)
saves the reasons and metrics with the signal and trade; "block" skips the setup / entry when it is sideways.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, InstrumentType, OrderType, Tick
from core.clock import combine, fmt_hm
from core.events import Category, Level
from core.indicators import ATR, EMA, RSI
from core.regime import RegimeConfig, check_regime, summarize
from strategies.base import Direction, EntrySignal, ExitPlan, PartialTarget, Strategy, StrategySettings, TradeInfo
from strategies.nifty_ref_breakout import (BSE_INDICES, DERIVATIVE_EXCHANGE, MARKET_CLOSE, MARKET_OPEN, atm_strike,
                                           pick_expiry)
from strategies.registry import register

TRAIL_MIN_STEP = 0.5  # points; smaller moves of the trailing stop are not sent
REGIME_CANDLES = 300  # completed candles kept for the sideways filter (enough for its EMA / ATR to settle)


class EmaRsiBreakoutSettings(StrategySettings):
    FORM_FIRST: ClassVar[tuple[str, ...]] = ("underlying", "exchange", "candle_timeframe_min", "lots")

    underlying: str = Field("NIFTY", title="Underlying index",
                            description="Index name as in the broker's instrument list (NIFTY, BANKNIFTY, SENSEX…)")
    exchange: Literal["NSE", "BSE"] = Field("NSE", title="Index exchange",
                                            description="NSE: options on NFO · BSE: options on BFO")
    candle_timeframe_min: int = Field(5, ge=1, le=60, title="Candle timeframe (minutes)")
    lots: int = Field(10, ge=1, le=200, title="Lots",
                      description="Lots per trade; lot size is read from the broker instrument master")
    # --- indicators (Pine inputs)
    ema_length: int = Field(21, ge=1, le=500, title="EMA length")
    rsi_length: int = Field(14, ge=1, le=200, title="RSI length")
    rsi_mid: float = Field(50, gt=0, lt=100, title="RSI middle level")
    # --- rules (Pine inputs)
    sync_bars: int = Field(3, ge=1, le=50, title="Max candles between EMA cross and RSI cross")
    valid_bars: int = Field(2, ge=1, le=50, title="Candles allowed for the breakout")
    sl_mode: Literal["confirmation_candle", "swing"] = Field(
        "confirmation_candle", title="Stop loss type",
        description="confirmation_candle: its low (long) / high (short) · swing: lowest low / highest high")
    swing_length: int = Field(10, ge=2, le=200, title="Swing high/low lookback (swing type only)")
    max_range_atr: float = Field(0, ge=0, le=20, title="Skip if confirmation candle range > x ATR (0 = off)")
    buffer_ticks: int = Field(1, ge=0, le=1000, title="Entry/stop buffer (ticks)")
    tick_size: float = Field(0.05, gt=0, le=10, title="Index tick size", description="NIFTY index: 0.05")
    early_exit_ema: bool = Field(True, title="Early exit: candle closes back through the EMA")
    early_exit_rsi: bool = Field(False, title="Also early exit if RSI goes back through the middle level")
    allow_long: bool = Field(True, title="Take long trades (buy CE)")
    allow_short: bool = Field(True, title="Take short trades (buy PE)")
    # --- platform rules
    entry_start: time = Field(time(9, 20), title="New entries from (IST)")
    entry_end: time = Field(time(15, 0), title="New entries until (IST)",
                            description="Open trades are closed by the final square-off (15:15)")
    tp_enabled: bool = Field(True, title="Use TP1 / TP2",
                             description="Off: the whole position exits at the RSI exit level (the script's rule)")
    tp1_rsi: float = Field(68, gt=0, lt=100, title="TP1: RSI level (long)",
                           description="Sell TP1 lots when RSI reaches this (live, mid-candle by default); "
                                       "short trades use 100 − this (68 -> 32)")
    tp1_lots: int = Field(0, ge=0, le=200, title="TP1 lots", description="0 = automatic (40 % of Lots: 10 -> 4)")
    tp2_rsi: float = Field(80, gt=0, lt=100, title="TP2: RSI level (long)",
                           description="Short trades use 100 − this (80 -> 20)")
    tp2_lots: int = Field(0, ge=0, le=200, title="TP2 lots", description="0 = automatic (40 % of Lots: 10 -> 4)")
    trail_after_tp1: bool = Field(
        True, title="Trail the stop after TP1",
        description="Once TP1 has sold, the spot stop-loss of the remaining lots follows the best price since "
                    "entry, `Trail distance` × ATR behind it (it only ever tightens). Chosen from a 41-day study: "
                    "about 3× the average result of keeping the original stop")
    trail_atr_mult: float = Field(1.5, ge=0.25, le=10, title="Trail distance (× ATR)",
                                  description="ATR(14) of the 5-minute candles; 1.5-2.0 tested best")
    rsi_exit: float = Field(70, gt=0, lt=100, title="RSI exit level when TP is off (long)",
                            description="Short trades use 100 − this (70 -> 30)")
    rsi_exit_on: Literal["tick", "candle_close"] = Field(
        "tick", title="RSI targets checked on",
        description="tick: TP1 / TP2 (or the RSI exit) fire mid-candle as soon as the live RSI reaches the level · "
                    "candle_close: only when a candle closes there (the Pine script's behaviour)")
    strike_step: int = Field(50, ge=1, le=1000, title="Strike step")
    expiry: Literal["nearest_weekly", "next_weekly"] = Field("nearest_weekly", title="Expiry")
    entry_order_type: Literal["MARKET", "LIMIT"] = Field("MARKET", title="Entry order type")
    limit_slippage_pct: float = Field(1.0, ge=0, le=20, title="LIMIT slippage (%)")
    protective_sl_pct: float = Field(0.0, ge=0, le=95, title="Protective SL-M (% of premium)",
                                     description="Optional broker-side disaster stop; 0 = off")
    warmup_candles: int = Field(300, ge=50, le=2000, title="Warm-up candles",
                                description="History loaded at start so EMA / RSI match the chart")
    # --- sideways (chop) filter, core/regime.py
    regime_filter: Literal["off", "log_only", "block"] = Field(
        "log_only", title="Sideways filter",
        description="log_only: record whether the market looked sideways next to every setup and trade, never "
                    "block · block: skip setups / entries when it does · off: not computed")
    regime_min_fails: int = Field(2, ge=1, le=5, title="Sideways: block when at least N checks fail",
                                  description="Checks: flat EMA, weak ADX, high Choppiness, many EMA crosses, tight box")
    regime_min_slope_atr: float = Field(0.3, ge=0, le=10, title="Sideways: min EMA slope (× ATR over 5 candles)")
    regime_min_adx: float = Field(20, ge=0, le=100, title="Sideways: min ADX(14)")
    regime_max_chop: float = Field(61.8, gt=0, le=100, title="Sideways: max Choppiness Index(14)")
    regime_max_crosses: int = Field(3, ge=0, le=20, title="Sideways: max EMA crosses in the last 20 candles")
    regime_min_range_atr: float = Field(2.5, ge=0, le=50, title="Sideways: min 1-hour box height (× ATR)")
    regime_block_against_slope: bool = Field(
        False, title="Sideways: also block trades against the EMA slope",
        description="CE only when the EMA slopes up, PE only when it slopes down (recorded either way)")

    @field_validator("underlying")
    @classmethod
    def _norm_underlying(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("underlying is required")
        return v

    @field_serializer("entry_start", "entry_end")
    def _ser_time(self, v: time) -> str:
        return fmt_hm(v)

    @model_validator(mode="after")
    def _checks(self) -> "EmaRsiBreakoutSettings":
        if self.underlying in BSE_INDICES and self.exchange != "BSE":
            raise ValueError(f"{self.underlying} trades on BSE: set Index exchange to BSE")
        if self.exchange == "BSE" and self.underlying not in BSE_INDICES:
            raise ValueError(f"{self.underlying} is not a BSE index: set Index exchange to NSE")
        if not (self.allow_long or self.allow_short):
            raise ValueError("allow long trades, short trades, or both")
        if not (MARKET_OPEN <= self.entry_start < self.entry_end <= MARKET_CLOSE):
            raise ValueError("entry window must be inside market hours 09:15-15:30, start before end")
        for name, lvl in (("TP1", self.tp1_rsi), ("TP2", self.tp2_rsi), ("RSI exit", self.rsi_exit)):
            if lvl <= self.rsi_mid:
                raise ValueError(f"{name} RSI level ({lvl:g}) must be above the middle level ({self.rsi_mid:g})")
        if self.tp_enabled:
            if self.tp2_rsi <= self.tp1_rsi:
                raise ValueError("TP2 RSI level must be greater than TP1 RSI level")
            if (self.tp1_lots == 0) != (self.tp2_lots == 0):
                raise ValueError("set both TP1 lots and TP2 lots to 0 (automatic split) or both to a number of lots")
            a, b = self.split()
            if a + b >= self.lots:
                raise ValueError(f"TP1 lots + TP2 lots ({a + b}) must be less than Lots ({self.lots}) so at least "
                                 f"one lot runs to the hard exit")
        return self

    def split(self) -> tuple[int, int]:
        if self.tp1_lots == 0 and self.tp2_lots == 0:
            n = self.lots * 2 // 5
            return n, n
        return self.tp1_lots, self.tp2_lots

    @property
    def buffer(self) -> float:
        return round(self.buffer_ticks * self.tick_size, 4)

    def regime_config(self) -> RegimeConfig:
        return RegimeConfig(ema_len=self.ema_length, min_fails=self.regime_min_fails,
                            min_slope_atr=self.regime_min_slope_atr, min_adx=self.regime_min_adx,
                            max_chop=self.regime_max_chop, max_crosses=self.regime_max_crosses,
                            min_range_atr=self.regime_min_range_atr)


@dataclass
class Setup:
    """A confirmation candle and its pending breakout order."""

    no: int
    direction: str  # CE_BUY | PE_BUY
    candle_start: datetime
    conf_high: float
    conf_low: float
    trigger: float
    stop: float
    valid_until: datetime  # close of the last candle allowed for the breakout
    state: str = "PENDING"  # PENDING | ENTERING | IN_TRADE | DONE | EXPIRED | CANCELLED | REJECTED
    result: str | None = None
    trade_id: str | None = None
    tp_done: list[str] = field(default_factory=list)
    exit_requested: bool = False
    best: float | None = None  # best spot price since entry (high for CE, low for PE)
    trail_stop: float | None = None  # current trailed stop, once moved
    regime: dict[str, Any] | None = None  # sideways filter when the setup armed
    regime_entry: dict[str, Any] | None = None  # ... and when the breakout fired

    @property
    def slot_id(self) -> str:
        return f"{self.candle_start.date().isoformat()}#S{self.no}"

    def to_state(self) -> dict[str, Any]:
        d = asdict(self)
        d["candle_start"], d["valid_until"] = self.candle_start.isoformat(), self.valid_until.isoformat()
        return d

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "Setup":
        d = dict(d)
        d["candle_start"] = datetime.fromisoformat(d["candle_start"])
        d["valid_until"] = datetime.fromisoformat(d["valid_until"])
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return {"no": self.no, "time": fmt_hm(self.candle_start), "direction": self.direction,
                "conf_high": self.conf_high, "conf_low": self.conf_low, "trigger": self.trigger, "stop": self.stop,
                "valid_until": fmt_hm(self.valid_until), "state": self.state, "result": self.result,
                "trade_id": self.trade_id, "tp_done": list(self.tp_done), "trail_stop": self.trail_stop,
                "regime": (self.regime_entry or self.regime or {}).get("note"),
                "regime_block": (self.regime_entry or self.regime or {}).get("would_block", False)}


class Indicators:
    """EMA / RSI / ATR plus the cross bookkeeping the Pine script uses (ta.crossover, ta.barssince)."""

    def __init__(self, s: EmaRsiBreakoutSettings) -> None:
        self.s = s
        self.ema, self.rsi, self.atr = EMA(s.ema_length), RSI(s.rsi_length), ATR(14)
        self.prev: tuple[float, float, float] | None = None  # (close, ema, rsi) of the previous candle
        self.since = {"px_up": None, "px_dn": None, "rsi_up": None, "rsi_dn": None}
        self.lows: list[float] = []
        self.highs: list[float] = []
        self.last: Candle | None = None
        self.candles: list[Candle] = []  # completed candles for the sideways filter, oldest first
        self.values: dict[str, float | None] = {"ema": None, "rsi": None, "atr": None}
        self.crosses: dict[str, bool] = {k: False for k in self.since}

    def update(self, c: Candle) -> None:
        ema, rsi, atr = self.ema.update(c.close), self.rsi.update(c.close), self.atr.update(c.high, c.low, c.close)
        cr = {k: False for k in self.since}
        if self.prev is not None and ema is not None and rsi is not None and None not in self.prev:
            pc, pe, pr = self.prev
            cr = {"px_up": c.close > ema and pc <= pe, "px_dn": c.close < ema and pc >= pe,
                  "rsi_up": rsi > self.s.rsi_mid and pr <= self.s.rsi_mid,
                  "rsi_dn": rsi < self.s.rsi_mid and pr >= self.s.rsi_mid}
        for k, hit in cr.items():
            self.since[k] = 0 if hit else (None if self.since[k] is None else self.since[k] + 1)
        self.crosses = cr
        self.prev = (c.close, ema, rsi)  # type: ignore[assignment]
        self.lows = (self.lows + [c.low])[-self.s.swing_length:]
        self.highs = (self.highs + [c.high])[-self.s.swing_length:]
        self.last = c
        self.candles.append(c)
        if len(self.candles) > REGIME_CANDLES + 50:
            del self.candles[:-REGIME_CANDLES]
        self.values = {"ema": ema, "rsi": rsi, "atr": atr}

    def ready(self) -> bool:
        return self.values["ema"] is not None and self.values["rsi"] is not None

    def within(self, key: str) -> bool:
        n = self.since[key]
        return n is not None and n < self.s.sync_bars


@register
class EmaRsiBreakout(Strategy):
    template_id: ClassVar[str] = "ema_rsi_breakout"
    display_name: ClassVar[str] = "NIFTY EMA21 + RSI50 Breakout + TP1/TP2 (Pine)"
    description: ClassVar[str] = ("From ema21_rsi50_candle_sl.pine: close across EMA 21 + RSI crossing 50 confirms a "
                                  "candle; buy ATM CE / PE on the break of its high / low within 2 candles; SL at "
                                  "the candle's other end; TP1 when live RSI hits 68 (PE 32), TP2 at 80 (PE 20), "
                                  "rest at the EMA early exit or the 15:15 hard exit.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = EmaRsiBreakoutSettings
    warmup_days: ClassVar[int] = 5  # backtests load this many earlier trading days for EMA / RSI warm-up

    def __init__(self, settings: EmaRsiBreakoutSettings, ctx: Any) -> None:
        super().__init__(settings, ctx)
        self.settings: EmaRsiBreakoutSettings = settings
        self.ind = Indicators(settings)
        self.spot: Instrument | None = None
        self.day: date | None = None
        self.setups: list[Setup] = []
        self.last_start: datetime | None = None
        self._saved: Any = None

    # ---- helpers
    @property
    def spot_key(self) -> InstrumentKey:
        return InstrumentKey(underlying=self.settings.underlying, exchange=Exchange(self.settings.exchange),
                             instrument_type=InstrumentType.INDEX)

    def subscriptions(self) -> list[InstrumentKey]:
        return [self.spot_key]

    def candle_timeframes(self) -> list[int]:
        return [self.settings.candle_timeframe_min]

    def _emit(self, event_type: str, message: str, level: Level = Level.INFO, **payload: Any) -> None:
        self.ctx.emit(event_type, message, level, Category.STRATEGY, **payload)

    @property
    def active(self) -> Setup | None:
        """The setup that is pending, entering or in a trade (at most one: one position at a time)."""
        return next((x for x in reversed(self.setups) if x.state in ("PENDING", "ENTERING", "IN_TRADE")), None)

    def _in_position(self) -> bool:
        a = self.active
        return a is not None and a.state in ("ENTERING", "IN_TRADE")

    # ---- persistence (restart keeps today's setups; never re-enters a setup that already signalled)
    def _persist(self) -> None:
        if self.day is None:
            return
        data = {"setups": [x.to_state() for x in self.setups], "last_start": self.last_start.isoformat()
                if self.last_start else None}
        if data == self._saved:
            return
        try:
            self.ctx.save_day_state(self.day, data)
            self._saved = data
        except Exception as exc:
            self._emit("SLOT_STATE_SAVE_FAILED", f"could not save state: {exc}", Level.WARNING)

    def _load(self, day: date) -> None:
        try:
            data = self.ctx.load_day_state(day) or {}
        except Exception:
            data = {}
        self.setups = [Setup.from_state(x) for x in data.get("setups") or []]
        now = self.ctx.now()
        for x in self.setups:
            if x.state == "ENTERING":  # the app stopped while entering: never enter this setup again
                x.state, x.result = "REJECTED", "interrupted_entry"
            elif x.state == "PENDING" and now >= x.valid_until:
                x.state, x.result = "EXPIRED", "app_was_off"

    # ---- lifecycle
    async def on_start(self) -> None:
        self.spot = self.ctx.resolve(self.spot_key)
        now = self.ctx.now()
        self.day = now.date()
        self._load(self.day)
        hist = await self.ctx.history(self.spot, self.settings.candle_timeframe_min, self.settings.warmup_candles)
        for c in hist:
            self.ind.update(c)
            self.last_start = c.start
        if hist:
            self._emit("WARMUP", f"indicators warmed up on {len(hist)} candles: EMA={self.ind.values['ema']} "
                                 f"RSI={self.ind.values['rsi']}", Level.DEBUG, candles=len(hist))
        else:
            self._emit("WARMUP_EMPTY", "no history: EMA / RSI start from the next candles (signals may be late)",
                       Level.WARNING)

    def restore(self, trades: list[TradeInfo]) -> None:
        by_slot = {t.slot_id: t for t in trades}
        for x in self.setups:
            t = by_slot.get(x.slot_id)
            if t is None:
                continue
            x.trade_id = t.trade_id
            if t.status == "OPEN":
                x.state = "IN_TRADE"
            elif x.state not in ("DONE",):
                x.state, x.result = "DONE", t.exit_reason or t.status.lower()

    async def on_timer(self, now: datetime) -> None:
        if self.day != now.date():  # new day: setups never carry over; indicators continue
            for x in self.setups:
                if x.state == "PENDING":
                    x.state, x.result = "EXPIRED", "day_end"
            self.day, self.setups = now.date(), []
        a = self.active
        if a is not None and a.state == "PENDING" and now >= combine(now.date(), self.settings.entry_end):
            a.state, a.result = "EXPIRED", "entry_window_closed"
        self.maybe_apply_pending()
        self._persist()

    # ---- candle close: indicators, exits, setups
    async def on_candle(self, instrument: Instrument, candle: Candle) -> None:
        s = self.settings
        if (self.spot is None or instrument.token != self.spot.token or candle.timeframe_min != s.candle_timeframe_min
                or (self.last_start is not None and candle.start <= self.last_start)):
            return
        if not getattr(candle, "complete", True):
            # built from ticks that started mid-candle (e.g. right after a restart): use the broker's candle
            try:
                full = await self.ctx.get_candle(self.spot, candle.start, s.candle_timeframe_min)
            except Exception:
                full = None
            if full is not None:
                candle = full
            else:
                self._emit("PARTIAL_CANDLE", f"{fmt_hm(candle.start)} candle is incomplete and the broker's is not "
                                             f"available yet; using it as is", Level.WARNING)
        flat = not self._in_position()  # Pine: arming needs a flat position before this candle's close orders
        self.ind.update(candle)
        self.last_start = candle.start
        close_time = candle.start + timedelta(minutes=s.candle_timeframe_min)
        if not self.ind.ready():
            return
        ema, rsi = self.ind.values["ema"], self.ind.values["rsi"]
        a = self.active
        # 1. exits decided on candle close
        if a is not None and a.state == "IN_TRADE" and a.trade_id and not a.exit_requested:
            await self._candle_exits(a, candle, ema, rsi)  # type: ignore[arg-type]
        # 2. pending breakout: void after `valid_bars` candles or a close back through the EMA
        if a is not None and a.state == "PENDING":
            back = candle.close < ema if a.direction == "CE_BUY" else candle.close > ema  # type: ignore[operator]
            if close_time >= a.valid_until or back:
                a.state, a.result = "EXPIRED", "ema_close_back" if back else "no_breakout"
                self._emit("SETUP_CANCELLED", f"setup #{a.no} {a.direction} {a.result}", setup=a.no, reason=a.result)
        # 3. new confirmation candle
        if flat and close_time < combine(close_time.date(), s.entry_end):
            self._arm(candle, close_time, ema, rsi)  # type: ignore[arg-type]
        self._persist()

    def _arm(self, c: Candle, close_time: datetime, ema: float, rsi: float) -> None:
        s, ind = self.settings, self.ind
        atr = ind.values["atr"]
        range_ok = s.max_range_atr == 0 or (atr is not None and (c.high - c.low) <= atr * s.max_range_atr)
        cr = ind.crosses
        long_conf = (s.allow_long and (cr["px_up"] or cr["rsi_up"]) and ind.within("px_up") and ind.within("rsi_up")
                     and c.close > ema and rsi > s.rsi_mid and range_ok)
        short_conf = (s.allow_short and (cr["px_dn"] or cr["rsi_dn"]) and ind.within("px_dn")
                      and ind.within("rsi_dn") and c.close < ema and rsi < s.rsi_mid and range_ok)
        if not (long_conf or short_conf):
            return
        old = self.active
        if old is not None and old.state == "PENDING":
            old.state, old.result = "CANCELLED", "replaced_by_new_setup"
        regime = self._regime("CE_BUY" if long_conf else "PE_BUY")
        buf = s.buffer
        if long_conf:
            d, trig = "CE_BUY", round(c.high + buf, 2)
            stop = round((c.low if s.sl_mode == "confirmation_candle" else min(ind.lows)) - buf, 2)
        else:
            d, trig = "PE_BUY", round(c.low - buf, 2)
            stop = round((c.high if s.sl_mode == "confirmation_candle" else max(ind.highs)) + buf, 2)
        x = Setup(no=len(self.setups) + 1, direction=d, candle_start=c.start, conf_high=c.high, conf_low=c.low,
                  trigger=trig, stop=stop,
                  valid_until=close_time + timedelta(minutes=s.candle_timeframe_min * s.valid_bars), regime=regime)
        self.setups.append(x)
        note = f" regime={regime['note']}" if regime else ""
        self._emit("SETUP", f"SETUP #{x.no} {d} candle={fmt_hm(c.start)} H={c.high:.2f} L={c.low:.2f} "
                            f"trigger={trig:.2f} SL={stop:.2f} EMA={ema:.2f} RSI={rsi:.2f} until={fmt_hm(x.valid_until)}"
                            f"{note}",
                   setup=x.no, direction=d, trigger=trig, stop=stop, ema=ema, rsi=rsi, regime=regime)
        if regime and regime["would_block"] and s.regime_filter == "block":
            x.state, x.result = "SKIPPED", "sideways"
            self._emit("REGIME_BLOCKED", f"setup #{x.no} {d} skipped: {regime['note']}", setup=x.no, regime=regime)

    def _regime(self, direction: str) -> dict[str, Any] | None:
        """The sideways filter on the completed candles (None when it is off)."""
        s = self.settings
        if s.regime_filter == "off":
            return None
        try:
            r = check_regime(self.ind.candles[-REGIME_CANDLES:], s.regime_config())
        except Exception as exc:  # never let the filter break the strategy; log-only data is just missing
            self._emit("REGIME_FAILED", f"sideways filter failed: {exc}", Level.WARNING)
            return None
        return summarize(r, direction, s.regime_block_against_slope, s.regime_filter)  # type: ignore[arg-type]

    async def _rsi_exits(self, a: Setup, rsi: float, where: str) -> bool:
        """TP1 / TP2 (or the single RSI exit when TP is off). True if something was sold."""
        s = self.settings
        is_long = a.direction == "CE_BUY"

        def reached(level: float) -> bool:
            return rsi >= level if is_long else rsi <= 100 - level

        if s.tp_enabled:
            fired = False
            for label, level in (("tp1", s.tp1_rsi), ("tp2", s.tp2_rsi)):
                if label not in a.tp_done and reached(level) and self._target_lots(label):
                    a.tp_done.append(label)
                    fired = True
                    self._emit("TP_SIGNAL", f"{label.upper()} RSI={rsi:.2f} ({where}) setup #{a.no}", setup=a.no,
                               rsi=rsi, where=where)
                    await self.ctx.request_exit(a.trade_id, label, partial=label)  # type: ignore[arg-type]
            return fired
        if reached(s.rsi_exit):
            a.exit_requested = True
            self._emit("RSI_EXIT_SIGNAL", f"RSI={rsi:.2f} ({where}) setup #{a.no}", setup=a.no, rsi=rsi, where=where)
            await self.ctx.request_exit(a.trade_id, f"rsi_{s.rsi_exit:g}")  # type: ignore[arg-type]
            return True
        return False

    async def _candle_exits(self, a: Setup, c: Candle, ema: float, rsi: float) -> None:
        s = self.settings
        is_long = a.direction == "CE_BUY"
        if await self._rsi_exits(a, rsi, "candle close"):
            return  # a target sold on this candle; the rest is judged on the next candles
        back_ema = s.early_exit_ema and (c.close < ema if is_long else c.close > ema)
        back_rsi = s.early_exit_rsi and (rsi < s.rsi_mid if is_long else rsi > s.rsi_mid)
        if back_ema or back_rsi:
            a.exit_requested = True
            reason = "early_exit_ema" if back_ema else "early_exit_rsi"
            self._emit("EARLY_EXIT_SIGNAL", f"{reason} setup #{a.no} close={c.close:.2f} EMA={ema:.2f} RSI={rsi:.2f}",
                       setup=a.no, reason=reason)
            await self.ctx.request_exit(a.trade_id, reason)  # type: ignore[arg-type]

    def _trail(self, a: Setup, price: float) -> None:
        """After TP1: keep the stop `trail_atr_mult` × ATR behind the best price since entry (only tightens)."""
        s = self.settings
        if a.trade_id is None or a.exit_requested:
            return
        is_long = a.direction == "CE_BUY"
        a.best = price if a.best is None else (max(a.best, price) if is_long else min(a.best, price))
        atr = self.ind.values["atr"]
        if not (s.tp_enabled and s.trail_after_tp1 and "tp1" in a.tp_done and atr):
            return
        level = round(a.best - atr * s.trail_atr_mult if is_long else a.best + atr * s.trail_atr_mult, 2)
        current = a.trail_stop if a.trail_stop is not None else a.stop
        # move only when it tightens by at least half a point (no order or event per tick)
        if (level >= current + TRAIL_MIN_STEP) if is_long else (level <= current - TRAIL_MIN_STEP):
            a.trail_stop = level
            self.ctx.tighten_stop(a.trade_id, level)
            self._persist()

    def _target_lots(self, label: str) -> int:
        a, b = self.settings.split()
        return a if label == "tp1" else b

    # ---- ticks: the breakout entry
    async def on_tick(self, tick: Tick) -> None:
        if self.spot is None or tick.token != self.spot.token:
            return
        a = self.active
        if a is None:
            return
        s = self.settings
        if a.state == "IN_TRADE":
            # mid-candle TP: the live RSI with this tick as the forming candle's close
            if s.rsi_exit_on == "tick" and a.trade_id and not a.exit_requested:
                rsi = self.ind.rsi.peek(tick.ltp)
                if rsi is not None and await self._rsi_exits(a, rsi, "live"):
                    self._persist()
            self._trail(a, tick.ltp)
            return
        if a.state != "PENDING":
            return
        now = self.ctx.now()
        if now >= a.valid_until or not (combine(now.date(), s.entry_start) <= now < combine(now.date(), s.entry_end)):
            return
        hit = tick.ltp >= a.trigger if a.direction == "CE_BUY" else tick.ltp <= a.trigger
        if hit:
            await self._enter(a, tick.ltp, now)
        self._persist()

    async def _enter(self, a: Setup, spot: float, now: datetime) -> None:
        s = self.settings
        a.state = "ENTERING"
        self._persist()  # saved before the order: a restart now never enters this setup again
        if self.ctx.has_open_position():
            a.state, a.result = "REJECTED", "already_in_position"
            return
        opt_exch = DERIVATIVE_EXCHANGE[s.exchange]
        expiry = pick_expiry(self.ctx.option_expiries(s.underlying, opt_exch), now.date(), s.expiry)
        if expiry is None:
            a.state, a.result = "REJECTED", "no_expiry_available"
            return
        a.regime_entry = self._regime(a.direction)
        if a.regime_entry and a.regime_entry["would_block"] and s.regime_filter == "block":
            a.state, a.result = "SKIPPED", "sideways_at_entry"
            self._emit("REGIME_BLOCKED", f"setup #{a.no} {a.direction} entry skipped: {a.regime_entry['note']}",
                       setup=a.no, regime=a.regime_entry)
            self._persist()
            return
        d = Direction(a.direction)
        lots1, lots2 = s.split()
        targets = ([PartialTarget(lbl, None, "above", n) for lbl, n in (("tp1", lots1), ("tp2", lots2)) if n > 0]
                   if s.tp_enabled else [])
        hard_exit = combine(now.date(), self.ctx.square_off_time())
        signal = EntrySignal(
            slot_id=a.slot_id, direction=d,
            option=InstrumentKey(underlying=s.underlying, exchange=opt_exch,
                                 instrument_type=InstrumentType.CE if d is Direction.CE_BUY else InstrumentType.PE,
                                 expiry=expiry, strike=atm_strike(spot, s.strike_step)),
            lots=s.lots, spot=spot, trigger=a.trigger, order_type=OrderType(s.entry_order_type),
            limit_slippage_pct=s.limit_slippage_pct, signal_time=now,
            exit_plan=ExitPlan(exit_at=hard_exit, underlying=self.spot, spot_sl=a.stop,
                               spot_sl_side="below" if d is Direction.CE_BUY else "above",
                               protective_sl_pct=s.protective_sl_pct or None, partial_targets=targets),
            meta={"setup_no": a.no, "underlying": s.underlying, "confirmation_candle": fmt_hm(a.candle_start),
                  "conf_high": a.conf_high, "conf_low": a.conf_low, "ema": self.ind.values["ema"],
                  "rsi": self.ind.values["rsi"], "buffer": s.buffer,
                  "regime": a.regime, "regime_entry": a.regime_entry},
        )
        info = await self.ctx.submit_entry(signal)
        if info is None:
            a.state, a.result = "REJECTED", "signal_rejected"
        else:
            a.trade_id, a.best = info.trade_id, spot
            a.state = "IN_TRADE" if info.status == "OPEN" else "DONE"
            if info.status != "OPEN":
                a.result = info.exit_reason or "entry_failed"
        self._persist()

    async def on_trade_update(self, trade: TradeInfo) -> None:
        for x in self.setups:
            if x.slot_id == trade.slot_id or (x.trade_id and x.trade_id == trade.trade_id):
                x.trade_id = trade.trade_id
                if trade.status == "OPEN":
                    x.state = "IN_TRADE"
                else:
                    x.state, x.result = "DONE", trade.exit_reason or trade.status.lower()
        self.maybe_apply_pending()
        self._persist()

    # ---- settings / UI
    def is_idle(self) -> bool:
        return self.active is None

    def apply_settings(self, new: StrategySettings) -> None:
        assert isinstance(new, EmaRsiBreakoutSettings)
        rebuild = (new.ema_length, new.rsi_length, new.candle_timeframe_min) != (
            self.settings.ema_length, self.settings.rsi_length, self.settings.candle_timeframe_min)
        same_tf = new.candle_timeframe_min == self.settings.candle_timeframe_min
        self.settings = new
        if rebuild:
            old = self.ind.candles if same_tf else []
            self.ind = Indicators(new)
            for c in old:  # same candles, new lengths: stays warmed up
                self.ind.update(c)
            if not old:
                self.last_start = None  # warmed up again on the next start
        else:
            self.ind.s = new

    def indicator_snapshot(self, price: float | None = None) -> dict[str, float] | None:
        """Live RSI (the forming candle closing at `price`, as the TP checks use) and the last EMA."""
        if not self.ind.ready():
            return None
        rsi = self.ind.rsi.peek(price) if price is not None else None
        rsi = self.ind.values["rsi"] if rsi is None else rsi
        return {"rsi": round(rsi, 2), "ema": round(self.ind.values["ema"], 2)}  # type: ignore[arg-type]

    def phase(self) -> str:
        a = self.active
        if a is None:
            return "WAITING"
        return "IN_TRADE" if a.state in ("ENTERING", "IN_TRADE") else "WATCHING"

    def status(self) -> dict[str, Any]:
        v = self.ind.values
        return {"day": self.day.isoformat() if self.day else None,
                "signals": [x.to_dict() for x in self.setups],
                "indicators": {"ema": round(v["ema"], 2) if v["ema"] is not None else None,
                               "rsi": round(v["rsi"], 2) if v["rsi"] is not None else None,
                               "candle": fmt_hm(self.ind.last.start) if self.ind.last else None,
                               "close": self.ind.last.close if self.ind.last else None},
                "pending_settings": self.has_pending_settings}
