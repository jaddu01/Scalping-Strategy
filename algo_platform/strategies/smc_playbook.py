"""SMC: liquidity sweep + market-structure shift + FVG / order-block retrace — from SMC_Strategy_Playbook.xlsx.

Works on any index the broker lists (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY on NFO; SENSEX, BANKEX on BFO): every
distance is measured in ATR, the strike step follows the index, and the expiry comes from the broker's list.

Two timeframes, both built from the index spot ticks:
  trend  (default 15 min)  swing highs / lows (fractal: `swing_strength` candles each side)
                           HH + HL = bullish, LH + LL = bearish, anything else = no trade
                           dealing range = last swing low .. last swing high; 50 % = equilibrium
  entry  (default 1 min)   everything below

Long (BUY the ATM CE); short (BUY the ATM PE) is the exact mirror:
  1 trend         bullish on the trend timeframe
  2 location      the sweep candle closes in DISCOUNT (below the equilibrium)
  3 sweep         its low goes below a liquidity level — a recent entry-timeframe swing low (last 5 hours), the
                  previous day's low or today's 09:15-09:30 opening-range low — and it closes back above that level
  4 displacement  within `confirm_window_min` minutes: a bullish candle whose body >= `displacement_atr` x ATR(14)
  5 MSS / CHOCH   within the same window: a close above the last swing high that existed at the sweep
                  (price trading below the sweep low first cancels the idea)
  6 zone          the newest bullish fair-value gap (candle 1 high < candle 3 low) formed since the sweep;
                  if none, the order block = the last bearish candle before the displacement
  7 entry         a candle trades into the zone (low <= zone top), closes bullish and above the zone bottom ->
                  BUY at that candle's close
  8 stop          spot: sweep low - `stop_buffer_atr` x ATR   (watched on every tick by the engine)
  9 target        spot: entry + `reward_risk` x risk           (whole position)
 11 cancel        a close below the zone bottom, `setup_valid_candles` candles without entry, the trend flips,
                  or the entry window ends
 12 management    `breakeven_at_1r`: once spot reaches +1R the stop moves to entry + `breakeven_lock_r` x risk
                  (0 = exactly the entry price; never widened);
                  `partial_lots_at_1r`: optionally sell that many lots at +1R
Continuation entries (`entry_model` = reversal_continuation, off by default) catch one-way trend days, where the
price stays in PREMIUM (long) and never sweeps a low. Long (short mirrors):
  c1 trend + place  bullish trend, the close above the equilibrium but at most `cont_max_stretch_atr` x the trend
                    timeframe's ATR above it
  c2 break          an entry candle closes above the latest swing high (the candle before did not) with a body >=
                    `displacement_atr` x ATR: a break of structure
  c3 zone           the newest bullish FVG since the impulse's swing low, else the order block (as step 6)
  c4 entry          the zone is touched, then within `cont_touch_candles` candles a bullish candle closes ABOVE the zone
  c5 stop           below the impulse's swing low (`cont_stop` = impulse) or the zone bottom (zone), - stop buffer
  c6 exit           `cont_exit` = trail_15m: no target; the stop follows the latest trend-timeframe swing low (minus the
                    stop buffer), only tightening; the rest exits at 15:15 · target_2r: the reward-risk target
                    `cont_breakeven` (off by default) applies the +1R breakeven move to continuation trades too
  limits            at most `cont_max_per_day` a day, new ones `cont_start`-`cont_end`; one position at a time
  (51-day NIFTY study, run 23's settings: reversal only +1.25 lakh; + continuation with impulse stop, 15-min trail,
  no breakeven +3.75 lakh, same drawdown; the gain comes from a few trend days)
Every setup that expires or is cancelled is logged (SETUP_ENDED) with the reason.
Platform rules: entries `entry_start`-`entry_end`, one position at a time, the 15:15 hard exit, risk checks, the
sideways filter (log-only by default).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, InstrumentType, OrderType, Tick
from core.clock import combine, fmt_hm
from core.events import Category, Level
from core.indicators import ATR
from strategies.base import Direction, EntrySignal, ExitPlan, PartialTarget, Strategy, StrategySettings, TradeInfo
from strategies.nifty_ref_breakout import (BSE_INDICES, DERIVATIVE_EXCHANGE, MARKET_CLOSE, MARKET_OPEN, atm_strike,
                                           pick_expiry)
from strategies.regime_filter import RegimeSettings, blocks, regime_of
from strategies.registry import register

STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100, "BANKEX": 100,
               "SENSEX50": 100}
OPENING_RANGE_MIN = 15  # 09:15-09:30
SWING_LOOKBACK_MIN = 300  # entry-timeframe swings older than 5 hours are not used as liquidity
KEEP_ENTRY_BARS = 1200
KEEP_TREND_BARS = 400
WARM_RETRY_FIRST, WARM_RETRY_MAX = 20.0, 300.0
TRAIL_MIN_STEP = 0.5  # continuation trail: smallest stop move, index points
WAITING = ("SWEPT", "ARMING", "ZONE", "CARMING", "CZONE")  # an idea not yet entered (CARMING / CZONE: continuation)


class SmcSettings(RegimeSettings):
    FORM_FIRST: ClassVar[tuple[str, ...]] = ("underlying", "exchange", "trend_timeframe_min", "entry_timeframe_min",
                                            "lots")

    underlying: str = Field("NIFTY", title="Underlying index",
                            description="As in the broker's instrument list: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, "
                                        "SENSEX, BANKEX")
    exchange: Literal["NSE", "BSE"] = Field("NSE", title="Index exchange",
                                            description="NSE: options on NFO · BSE (SENSEX, BANKEX): options on BFO")
    trend_timeframe_min: int = Field(15, ge=1, le=240, title="Trend timeframe (minutes)",
                                     description="Market structure (HH/HL, LH/LL) and premium / discount. The playbook "
                                                 "says H1; 60 made 2-5 trades in 60 days in the study, 15 about one a day")
    entry_timeframe_min: int = Field(1, ge=1, le=60, title="Entry timeframe (minutes)",
                                     description="Sweep, displacement, MSS, FVG / order block and the entry candle")
    lots: int = Field(10, ge=1, le=200, title="Lots", description="Lot size is read from the broker instrument master")
    swing_strength: int = Field(2, ge=1, le=10, title="Swing strength (candles each side)",
                                description="A swing high is the highest high with this many candles on each side")
    displacement_atr: float = Field(1.2, ge=0.1, le=10, title="Displacement: body ≥ x × ATR(14)")
    confirm_window_min: int = Field(50, ge=1, le=600, title="Displacement + MSS within (minutes after the sweep)")
    setup_valid_candles: int = Field(30, ge=1, le=500, title="Entry within (candles after the zone)",
                                     description="No retrace into the zone in time cancels the setup")
    stop_buffer_atr: float = Field(0.2, ge=0, le=5, title="Stop buffer beyond the sweep (× ATR)")
    reward_risk: float = Field(2.0, ge=0.5, le=10, title="Target (× risk, R:R)")
    breakeven_at_1r: bool = Field(True, title="Move stop to breakeven at +1R",
                                  description="Once spot is one risk-unit in profit the stop moves to the entry price")
    breakeven_lock_r: float = Field(0.3, ge=0, le=0.9, title="At +1R, lock profit of x R (0 = entry)",
                                    description="At +1R the stop moves to entry + x × risk instead of exactly entry; "
                                                "0.3 kept ~Rs 21k more over 46 days without cutting a 2R winner")
    partial_lots_at_1r: int = Field(0, ge=0, le=200, title="Sell lots at +1R (0 = off)")
    max_stop_atr: float = Field(0, ge=0, le=100, title="Skip if stop is wider than x × ATR (0 = off)",
                                description="Very wide stops are expensive for option buying")
    use_prev_day_levels: bool = Field(True, title="Liquidity: previous day high / low")
    use_opening_range: bool = Field(True, title="Liquidity: opening range 09:15-09:30 high / low")
    allow_long: bool = Field(True, title="Take long trades (buy CE)")
    allow_short: bool = Field(True, title="Take short trades (buy PE)")
    entry_start: time = Field(time(9, 30), title="New entries from (IST)")
    entry_end: time = Field(time(14, 45), title="New entries until (IST)",
                            description="Open trades are closed by the final square-off (15:15)")
    strike_step: int = Field(0, ge=0, le=1000, title="Strike step (0 = automatic for the index)",
                             description="NIFTY 50 · BANKNIFTY 100 · FINNIFTY 50 · MIDCPNIFTY 25 · SENSEX 100 · BANKEX 100")
    expiry: Literal["nearest_weekly", "next_weekly"] = Field(
        "nearest_weekly", title="Expiry", description="Nearest listed expiry (monthly-only indices use the monthly)")
    entry_order_type: Literal["MARKET", "LIMIT"] = Field("MARKET", title="Entry order type")
    limit_slippage_pct: float = Field(1.0, ge=0, le=20, title="LIMIT slippage (%)")
    protective_sl_pct: float = Field(0.0, ge=0, le=95, title="Protective SL-M (% of premium, 0 = off)")
    warmup_trend_candles: int = Field(300, ge=50, le=2000, title="Warm-up candles (trend timeframe)")
    warmup_entry_candles: int = Field(800, ge=100, le=5000, title="Warm-up candles (entry timeframe)")
    entry_model: Literal["reversal", "reversal_continuation"] = Field(
        "reversal", title="Entry model",
        description="reversal: sweep in discount / premium + MSS (the playbook) · reversal_continuation: also "
                    "trend-continuation entries (break of structure in premium + retrace), for one-way trend days")
    cont_stop: Literal["impulse", "zone"] = Field(
        "impulse", title="Continuation stop", description="impulse: below the impulse's swing low · zone: below the "
                                                          "zone bottom (tighter, stopped out more often)")
    cont_exit: Literal["trail_15m", "target_2r"] = Field(
        "trail_15m", title="Continuation exit",
        description="trail_15m: no target, the stop follows the trend timeframe's swing lows (highs), the rest at 15:15 "
                    "· target_2r: the Target (× risk) above")
    cont_breakeven: bool = Field(False, title="Continuation: breakeven at +1R",
                                 description="Off: continuation trades keep their stop / trail (the move to breakeven "
                                             "cut trend-day winners in the study)")
    cont_max_per_day: int = Field(2, ge=1, le=10, title="Continuation trades per day (max)")
    cont_start: time = Field(time(9, 45), title="Continuation entries from (IST)")
    cont_end: time = Field(time(14, 30), title="Continuation entries until (IST)")
    cont_max_stretch_atr: float = Field(3.0, gt=0, le=20, title="Continuation: skip if more than x × trend ATR past the "
                                                               "equilibrium")
    cont_touch_candles: int = Field(3, ge=1, le=20, title="Continuation: entry within x candles after the zone touch")

    @field_validator("underlying")
    @classmethod
    def _norm_underlying(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("underlying is required")
        return v

    @field_serializer("entry_start", "entry_end", "cont_start", "cont_end")
    def _ser_time(self, v: time) -> str:
        return fmt_hm(v)

    @model_validator(mode="after")
    def _checks(self) -> "SmcSettings":
        if self.underlying in BSE_INDICES and self.exchange != "BSE":
            raise ValueError(f"{self.underlying} trades on BSE: set Index exchange to BSE")
        if self.exchange == "BSE" and self.underlying not in BSE_INDICES:
            raise ValueError(f"{self.underlying} is not a BSE index: set Index exchange to NSE")
        if not (self.allow_long or self.allow_short):
            raise ValueError("allow long trades, short trades, or both")
        if self.trend_timeframe_min <= self.entry_timeframe_min:
            raise ValueError("the trend timeframe must be longer than the entry timeframe")
        if not (MARKET_OPEN <= self.entry_start < self.entry_end <= MARKET_CLOSE):
            raise ValueError("entry window must be inside market hours 09:15-15:30, start before end")
        if self.partial_lots_at_1r >= self.lots:
            raise ValueError("lots sold at +1R must be fewer than Lots")
        if self.strike_step == 0 and self.underlying not in STRIKE_STEP:
            raise ValueError(f"no automatic strike step for {self.underlying}: set Strike step")
        if self.entry_model == "reversal_continuation" and not (self.cont_start < self.cont_end):
            raise ValueError("continuation entries: the start must be before the end")
        return self

    @property
    def step(self) -> int:
        return self.strike_step or STRIKE_STEP[self.underlying]


@dataclass
class Bar:
    n: int  # running number of the candle on its timeframe (never reused)
    start: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def bull(self) -> bool:
        return self.close > self.open

    @property
    def bear(self) -> bool:
        return self.close < self.open


@dataclass
class Setup:
    no: int
    side: str  # CE_BUY | PE_BUY
    sweep_time: datetime
    sweep_n: int
    sweep_px: float  # the sweep candle's low (long) / high (short)
    level: float  # the liquidity level it swept
    level_kind: str  # swing | prev_day | opening_range
    ref_swing: float  # the swing high (long) / low (short) whose break is the MSS
    atr: float
    state: str = "SWEPT"  # SWEPT | ARMING | ZONE | ENTERING | IN_TRADE | DONE | EXPIRED | CANCELLED | SKIPPED | REJECTED
    disp_n: int | None = None
    mss_n: int | None = None
    ready_n: int | None = None
    zone_kind: str | None = None  # FVG | OB
    zone_lo: float | None = None
    zone_hi: float | None = None
    entry_spot: float | None = None
    stop: float | None = None
    target: float | None = None
    one_r: float | None = None
    be_moved: bool = False
    trade_id: str | None = None
    result: str | None = None
    entry_time: datetime | None = None
    regime: dict[str, Any] | None = None
    touch_n: int | None = None  # continuation: the candle that last touched the zone

    @property
    def long(self) -> bool:
        return self.side == "CE_BUY"

    @property
    def continuation(self) -> bool:
        return self.level_kind == "continuation"

    @property
    def slot_id(self) -> str:
        return f"{self.sweep_time.date().isoformat()}#M{self.no}"

    def to_state(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("sweep_time", "entry_time"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "Setup":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        for k in ("sweep_time", "entry_time"):
            d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return {"no": self.no, "time": fmt_hm(self.sweep_time), "direction": self.side, "state": self.state,
                "sweep": round(self.sweep_px, 2), "level": round(self.level, 2), "level_kind": self.level_kind,
                "mss_level": round(self.ref_swing, 2),
                "zone": (f"{self.zone_kind} {self.zone_lo:.2f}–{self.zone_hi:.2f}" if self.zone_kind else None),
                "entry": self.entry_spot, "stop": self.stop, "target": self.target, "be": self.be_moved,
                "entry_time": fmt_hm(self.entry_time) if self.entry_time else None,
                "trade_id": self.trade_id, "result": self.result,
                "regime": (self.regime or {}).get("note"), "regime_block": bool((self.regime or {}).get("would_block"))}


class Series:
    """Completed candles of one timeframe with confirmed fractal swings."""

    def __init__(self, keep: int, strength: int) -> None:
        self.bars: list[Bar] = []
        self.keep, self.k = keep, strength
        self.swings: list[tuple[int, int, float, str]] = []  # (bar n, confirmed at n, price, "H" | "L")
        self.atr = ATR(14)
        self.atr_value: float | None = None
        self.n = 0

    def add(self, c: Candle) -> Bar:
        b = Bar(self.n, c.start, c.open, c.high, c.low, c.close)
        self.n += 1
        self.bars.append(b)
        self.atr_value = self.atr.update(c.high, c.low, c.close) or self.atr_value
        k = self.k
        if len(self.bars) >= 2 * k + 1:  # the candle k bars back is now a confirmed swing if it is the extreme
            win = self.bars[-(2 * k + 1):]
            mid, left, right = win[k], win[:k], win[k + 1:]
            # strictly beyond the candles before it, at least equal to those after: a tie counts once
            if mid.high > max(x.high for x in left) and mid.high >= max(x.high for x in right):
                self.swings.append((mid.n, b.n, mid.high, "H"))
            if mid.low < min(x.low for x in left) and mid.low <= min(x.low for x in right):
                self.swings.append((mid.n, b.n, mid.low, "L"))
        if len(self.bars) > self.keep + 200:
            del self.bars[:-self.keep]
            first = self.bars[0].n
            self.swings = [s for s in self.swings if s[0] >= first]
        return b

    def get(self, n: int) -> Bar | None:
        i = n - self.bars[0].n if self.bars else -1
        return self.bars[i] if 0 <= i < len(self.bars) else None

    def last_swings(self, kind: str, count: int, known_by: int | None = None, since: int | None = None) -> list[float]:
        return [s[2] for s in self.swings if s[3] == kind and (known_by is None or s[1] <= known_by)
                and (since is None or s[0] >= since)][-count:]


@register
class SmcPlaybook(Strategy):
    template_id: ClassVar[str] = "smc_playbook"
    display_name: ClassVar[str] = "SMC: Sweep + MSS + FVG/OB (any index)"
    description: ClassVar[str] = ("SMC_Strategy_Playbook.xlsx: trend from swing structure, liquidity sweep in "
                                  "discount / premium, displacement + MSS, retrace into the FVG or order block, buy "
                                  "the ATM CE / PE; stop beyond the sweep, 2R target, breakeven at +1R.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = SmcSettings
    warmup_days: ClassVar[int] = 5

    def __init__(self, settings: SmcSettings, ctx: Any) -> None:
        super().__init__(settings, ctx)
        self.settings: SmcSettings = settings
        self._new_series()
        self.spot: Instrument | None = None
        self.day: date | None = None
        self.setups: list[Setup] = []
        self.day_hl: dict[date, list[float]] = {}  # entry-timeframe high / low per day (previous-day levels)
        self.cold = False
        self._warm_retry_at: datetime | None = None
        self._warm_wait = WARM_RETRY_FIRST
        self._saved: Any = None

    def _new_series(self) -> None:
        s = self.settings
        self.entry = Series(KEEP_ENTRY_BARS, s.swing_strength)
        self.trend_s = Series(KEEP_TREND_BARS, s.swing_strength)
        self.last_start: dict[int, datetime] = {}

    # ---- basics
    @property
    def spot_key(self) -> InstrumentKey:
        return InstrumentKey(underlying=self.settings.underlying, exchange=Exchange(self.settings.exchange),
                             instrument_type=InstrumentType.INDEX)

    def subscriptions(self) -> list[InstrumentKey]:
        return [self.spot_key]

    def candle_timeframes(self) -> list[int]:
        return sorted({self.settings.entry_timeframe_min, self.settings.trend_timeframe_min})

    def _emit(self, event_type: str, message: str, level: Level = Level.INFO, **payload: Any) -> None:
        self.ctx.emit(event_type, message, level, Category.STRATEGY, **payload)

    @property
    def active(self) -> Setup | None:
        return next((x for x in reversed(self.setups) if x.state in ("ENTERING", "IN_TRADE")), None)

    def trend(self) -> tuple[str, float | None, float | None]:
        """bull / bear / side and the dealing range (last swing high, last swing low) on the trend timeframe."""
        hs, ls = self.trend_s.last_swings("H", 2), self.trend_s.last_swings("L", 2)
        if len(hs) < 2 or len(ls) < 2:
            return "none", None, None
        if hs[1] > hs[0] and ls[1] > ls[0]:
            return "bull", hs[1], ls[1]
        if hs[1] < hs[0] and ls[1] < ls[0]:
            return "bear", hs[1], ls[1]
        return "side", hs[1], ls[1]

    def _end(self, x: Setup, state: str, result: str) -> None:
        """An idea that will not be traded: kept in the setups table and logged with the reason."""
        x.state, x.result = state, result
        self._emit("SETUP_ENDED", f"#{x.no} {x.side}{' continuation' if x.continuation else ''} {state.lower()}: "
                                  f"{result.replace('_', ' ')}", setup=x.no, state=state, reason=result)

    # ---- persistence: a restart keeps today's finished / open setups, never re-enters one that signalled
    def _persist(self) -> None:
        if self.day is None:
            return
        data = {"setups": [x.to_state() for x in self.setups]}
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
        for x in self.setups:
            if x.state == "ENTERING":
                x.state, x.result = "REJECTED", "interrupted_entry"
            elif x.state in WAITING:  # candle numbers restart: the idea is rebuilt from scratch
                x.state, x.result = "EXPIRED", "app_restarted"

    # ---- lifecycle
    async def on_start(self) -> None:
        self.spot = self.ctx.resolve(self.spot_key)
        now = self.ctx.now()
        self.day = now.date()
        self._load(self.day)
        await self._try_warm_up(now, first=True)

    async def _try_warm_up(self, now: datetime, first: bool = False) -> bool:
        s = self.settings
        try:
            trend = await self.ctx.history(self.spot, s.trend_timeframe_min, s.warmup_trend_candles)
            entry = await self.ctx.history(self.spot, s.entry_timeframe_min, s.warmup_entry_candles)
        except Exception:
            trend, entry = [], []
        need_entry = 2 * (375 // s.entry_timeframe_min) // 3  # about two thirds of a day
        if len(trend) >= 4 * (2 * s.swing_strength + 1) and len(entry) >= need_entry:
            self._new_series()
            self.day_hl = {}
            for c in trend:
                self.trend_s.add(c)
            for c in entry:
                self._add_entry(c)
            self.last_start = {s.trend_timeframe_min: trend[-1].start, s.entry_timeframe_min: entry[-1].start}
            t, hi, lo = self.trend()
            self._emit("WARMUP", f"history: {len(trend)} x {s.trend_timeframe_min}m, {len(entry)} x "
                                 f"{s.entry_timeframe_min}m; trend={t} range={lo}-{hi}"
                                 + ("; setups resume" if self.cold else ""),
                       Level.WARNING if self.cold else Level.DEBUG)
            self.cold, self._warm_wait = False, WARM_RETRY_FIRST
            return True
        if first or not self.cold:
            self._emit("WARMUP_EMPTY", f"history not available ({len(trend)} trend / {len(entry)} entry candles): "
                                       f"no setups until it loads; retrying", Level.WARNING)
        self.cold = True
        self._warm_retry_at = now + timedelta(seconds=self._warm_wait)
        self._warm_wait = min(self._warm_wait * 2, WARM_RETRY_MAX)
        return False

    def restore(self, trades: list[TradeInfo]) -> None:
        by_slot = {t.slot_id: t for t in trades}
        for x in self.setups:
            t = by_slot.get(x.slot_id)
            if t is None:
                continue
            x.trade_id = t.trade_id
            if t.status == "OPEN":
                x.state = "IN_TRADE"
            elif x.state != "DONE":
                x.state, x.result = "DONE", t.exit_reason or t.status.lower()

    async def on_timer(self, now: datetime) -> None:
        if self.day != now.date():
            for x in self.setups:
                if x.state in WAITING:
                    self._end(x, "EXPIRED", "day_end")
            self.day, self.setups, self._saved = now.date(), [], None
        if now >= combine(now.date(), self.settings.entry_end):
            for x in self.setups:
                if x.state in WAITING:
                    self._end(x, "EXPIRED", "entry_window_closed")
        if self.cold and self.spot is not None and (self._warm_retry_at is None or now >= self._warm_retry_at):
            await self._try_warm_up(now)
        self.maybe_apply_pending()
        self._persist()

    # ---- candles
    def _add_entry(self, c: Candle) -> Bar:
        b = self.entry.add(c)
        hl = self.day_hl.setdefault(c.start.date(), [c.high, c.low])
        hl[0], hl[1] = max(hl[0], c.high), min(hl[1], c.low)
        return b

    async def on_candle(self, instrument: Instrument, candle: Candle) -> None:
        s = self.settings
        tf = candle.timeframe_min
        if self.spot is None or instrument.token != self.spot.token or tf not in (s.entry_timeframe_min,
                                                                                    s.trend_timeframe_min):
            return
        last = self.last_start.get(tf)
        if last is not None and candle.start <= last:
            return
        if not getattr(candle, "complete", True):
            try:
                full = await self.ctx.get_candle(self.spot, candle.start, tf)
            except Exception:
                full = None
            candle = full or candle
        self.last_start[tf] = candle.start
        if tf == s.trend_timeframe_min:
            self.trend_s.add(candle)
            if tf != s.entry_timeframe_min:
                return
        bar = self._add_entry(candle) if tf == s.entry_timeframe_min else None
        if bar is None or self.cold:
            return  # cold: no decisions until the history (and with it the real structure) is loaded
        close_time = candle.start + timedelta(minutes=tf)
        await self._advance(bar, close_time)
        if self.active is None and close_time < combine(close_time.date(), s.entry_end):
            self._detect_sweep(bar, close_time)
        if s.entry_model == "reversal_continuation":
            await self._cont_advance(bar, close_time)
            if self.active is None:
                self._cont_detect(bar, close_time)
        self._cont_trail()
        self._persist()

    def _detect_sweep(self, b: Bar, close_time: datetime) -> None:
        s = self.settings
        trend, hi, lo = self.trend()
        if trend not in ("bull", "bear") or hi is None or lo is None:
            return
        if (trend == "bull" and not s.allow_long) or (trend == "bear" and not s.allow_short):
            return
        long = trend == "bull"
        eq = (hi + lo) / 2
        if (b.close >= eq) if long else (b.close <= eq):
            return  # not in discount (long) / premium (short)
        tf = s.entry_timeframe_min
        day = b.start.date()
        or_end = combine(day, MARKET_OPEN) + timedelta(minutes=OPENING_RANGE_MIN)
        if close_time <= or_end:
            return  # the opening range is still forming
        levels: list[tuple[float, str]] = []
        since = b.n - SWING_LOOKBACK_MIN // tf
        kind = "L" if long else "H"
        levels += [(p, "swing") for p in self.entry.last_swings(kind, 3, known_by=b.n - 1, since=since)]
        prev_days = sorted(d for d in self.day_hl if d < day)
        if s.use_prev_day_levels and prev_days:
            ph, pl = self.day_hl[prev_days[-1]]
            levels.append((pl if long else ph, "prev_day"))
        if s.use_opening_range:
            orb = [x for x in self.entry.bars if x.start.date() == day and x.start < or_end]
            if orb:
                levels.append(((min(x.low for x in orb), "opening_range") if long else
                               (max(x.high for x in orb), "opening_range")))
        swept = [(lv, k) for lv, k in levels if (b.low < lv < b.close if long else b.close < lv < b.high)]
        if not swept:
            return
        ref = self.entry.last_swings("H" if long else "L", 1, known_by=b.n - 1, since=since)
        if not ref or self.entry.atr_value is None:
            return
        if any(x.state in ("SWEPT", "ARMING", "ZONE") and x.sweep_n == b.n for x in self.setups):
            return
        lv, k = swept[0]
        x = Setup(no=len(self.setups) + 1, side="CE_BUY" if long else "PE_BUY", sweep_time=b.start, sweep_n=b.n,
                  sweep_px=b.low if long else b.high, level=lv, level_kind=k, ref_swing=ref[-1],
                  atr=self.entry.atr_value)
        self.setups.append(x)
        self._emit("SWEEP", f"SWEEP #{x.no} {x.side} {fmt_hm(b.start)} {'low' if long else 'high'}="
                            f"{x.sweep_px:.2f} swept {k} {lv:.2f}, closed {b.close:.2f}; trend={trend} range="
                            f"{lo:.2f}-{hi:.2f} eq={eq:.2f}; MSS level {x.ref_swing:.2f}", setup=x.no)
        # the sweep candle itself can already be the displacement / MSS candle
        self._check_confirm(x, b)

    def _check_confirm(self, x: Setup, b: Bar) -> None:
        s = self.settings
        long = x.long
        atr_prev = self.entry.atr_value or x.atr
        if x.disp_n is None and abs(b.close - b.open) >= s.displacement_atr * atr_prev and (b.bull if long else b.bear):
            x.disp_n = b.n
        if x.mss_n is None and ((b.close > x.ref_swing) if long else (b.close < x.ref_swing)):
            x.mss_n = b.n
        if x.disp_n is not None and x.mss_n is not None:
            x.state, x.ready_n = "ARMING", max(x.disp_n, x.mss_n) + 1
            self._emit("MSS", f"MSS #{x.no} {x.side}: displacement {fmt_hm(self.entry.get(x.disp_n).start)}, "
                              f"structure break {fmt_hm(self.entry.get(x.mss_n).start)}", setup=x.no)

    def _find_zone(self, x: Setup, b: Bar) -> bool:
        long = x.long
        for n in range(b.n, x.sweep_n + 1, -1):  # newest fair-value gap since the sweep
            c1, c3 = self.entry.get(n - 2), self.entry.get(n)
            if c1 is None or c3 is None or c1.n < x.sweep_n:
                continue
            if long and c1.high < c3.low:
                x.zone_kind, x.zone_lo, x.zone_hi = "FVG", c1.high, c3.low
                return True
            if not long and c1.low > c3.high:
                x.zone_kind, x.zone_lo, x.zone_hi = "FVG", c3.high, c1.low
                return True
        for n in range((x.disp_n or b.n) - 1, x.sweep_n - 1, -1):  # else the order block
            c = self.entry.get(n)
            if c is not None and (c.bear if long else c.bull):
                x.zone_kind, x.zone_lo, x.zone_hi = "OB", c.low, c.high
                return True
        return False

    async def _advance(self, b: Bar, close_time: datetime) -> None:
        s = self.settings
        window = max(1, s.confirm_window_min // s.entry_timeframe_min)
        trend = self.trend()[0]
        for x in self.setups:
            if x.state == "SWEPT":
                if (b.low < x.sweep_px) if x.long else (b.high > x.sweep_px):
                    self._end(x, "CANCELLED", "beyond_sweep")
                elif b.n - x.sweep_n > window:
                    self._end(x, "EXPIRED", "no_displacement_mss")
                elif b.n > x.sweep_n:
                    self._check_confirm(x, b)
                continue
            if x.state == "ARMING" and x.ready_n is not None and b.n >= x.ready_n:
                if not self._find_zone(x, b):
                    self._end(x, "EXPIRED", "no_zone")
                    continue
                x.state = "ZONE"
                self._emit("ZONE", f"ZONE #{x.no} {x.side} {x.zone_kind} {x.zone_lo:.2f}-{x.zone_hi:.2f}", setup=x.no)
            if x.state == "ZONE":
                lo, hi = x.zone_lo, x.zone_hi
                if trend != ("bull" if x.long else "bear"):
                    self._end(x, "CANCELLED", "trend_changed")
                elif (b.close < lo) if x.long else (b.close > hi):  # type: ignore[operator]
                    self._end(x, "CANCELLED", "closed_through_zone")
                elif b.n - (x.ready_n or b.n) >= s.setup_valid_candles:
                    self._end(x, "EXPIRED", "no_retrace")
                elif ((b.low <= hi and b.bull and b.close > lo) if x.long  # type: ignore[operator]
                      else (b.high >= lo and b.bear and b.close < hi)):  # type: ignore[operator]
                    if self.active is None:
                        await self._enter(x, b, close_time)

    # ---- continuation entries (trend days: break of structure in premium + retrace)
    def _cont_today(self, day: date) -> int:
        return sum(1 for x in self.setups if x.continuation and x.sweep_time.date() == day
                   and x.state in ("ENTERING", "IN_TRADE", "DONE"))

    def _cont_detect(self, b: Bar, close_time: datetime) -> None:
        s = self.settings
        if not (s.cont_start <= close_time.time() <= s.cont_end) or self._cont_today(close_time.date()) >= s.cont_max_per_day:
            return
        trend, hi, lo = self.trend()
        if trend not in ("bull", "bear") or hi is None or lo is None:
            return
        long = trend == "bull"
        if (long and not s.allow_long) or (not long and not s.allow_short):
            return
        eq, tatr, atr = (hi + lo) / 2, self.trend_s.atr_value, self.entry.atr_value
        if not tatr or not atr:
            return
        stretch = s.cont_max_stretch_atr * tatr
        if (not eq < b.close <= eq + stretch) if long else (not eq - stretch <= b.close < eq):
            return  # continuation only in premium (long) / discount (short), and not too far from the equilibrium
        if abs(b.close - b.open) < s.displacement_atr * atr or not (b.bull if long else b.bear):
            return
        sw = [x for x in self.entry.swings if x[3] == ("H" if long else "L") and x[1] <= b.n - 1]
        opp = [x for x in self.entry.swings if x[3] == ("L" if long else "H") and x[1] <= b.n - 1]
        prev = self.entry.get(b.n - 1)
        if not sw or not opp or prev is None:
            return
        lvl = sw[-1][2]
        if not ((b.close > lvl >= prev.close) if long else (b.close < lvl <= prev.close)):
            return  # no fresh break of structure on this candle
        if any(x.continuation and x.ref_swing == lvl and x.sweep_time.date() == b.start.date() for x in self.setups):
            return
        x = Setup(no=len(self.setups) + 1, side="CE_BUY" if long else "PE_BUY", sweep_time=b.start, sweep_n=opp[-1][0],
                  sweep_px=opp[-1][2], level=lvl, level_kind="continuation", ref_swing=lvl, atr=atr, state="CARMING",
                  disp_n=b.n, mss_n=b.n, ready_n=b.n + 1)
        self.setups.append(x)
        self._emit("BOS", f"BOS #{x.no} {x.side} continuation {fmt_hm(b.start)} close={b.close:.2f} broke "
                          f"{'swing high' if long else 'swing low'} {lvl:.2f}; impulse from {x.sweep_px:.2f}; trend="
                          f"{trend} eq={eq:.2f}", setup=x.no)

    async def _cont_advance(self, b: Bar, close_time: datetime) -> None:
        s = self.settings
        trend = self.trend()[0]
        for x in self.setups:
            if not x.continuation:
                continue
            if x.state == "CARMING" and x.ready_n is not None and b.n >= x.ready_n:
                if not self._find_zone(x, b):
                    self._end(x, "EXPIRED", "no_zone")
                    continue
                x.state = "CZONE"
                self._emit("ZONE", f"ZONE #{x.no} {x.side} continuation {x.zone_kind} {x.zone_lo:.2f}-{x.zone_hi:.2f}",
                           setup=x.no)
            if x.state != "CZONE":
                continue
            lo, hi = x.zone_lo, x.zone_hi
            if trend != ("bull" if x.long else "bear"):
                self._end(x, "CANCELLED", "trend_changed")
            elif (b.close < lo) if x.long else (b.close > hi):  # type: ignore[operator]
                self._end(x, "CANCELLED", "closed_through_zone")
            elif b.n - (x.ready_n or b.n) >= s.setup_valid_candles:
                self._end(x, "EXPIRED", "no_retrace")
            else:
                if (b.low <= hi) if x.long else (b.high >= lo):  # type: ignore[operator]
                    x.touch_n = b.n
                if (x.touch_n is not None and b.n - x.touch_n <= s.cont_touch_candles
                        and ((b.bull and b.close > hi) if x.long else (b.bear and b.close < lo))  # type: ignore[operator]
                        and self.active is None):
                    if s.cont_stop == "zone":
                        x.sweep_px = lo if x.long else hi  # type: ignore[assignment]
                    x.state = "ZONE"
                    await self._enter(x, b, close_time)

    def _cont_trail(self) -> None:
        """Continuation trade, trail_15m exit: the stop follows the latest trend-timeframe swing low (high) minus the
        stop buffer; it only tightens (the engine refuses anything else too)."""
        s, a = self.settings, self.active
        if a is None or not a.continuation or a.state != "IN_TRADE" or not a.trade_id or s.cont_exit != "trail_15m":
            return
        sw = [x for x in self.trend_s.swings if x[3] == ("L" if a.long else "H")]
        atr = self.entry.atr_value or a.atr
        if not sw or a.stop is None:
            return
        cand = round(sw[-1][2] - s.stop_buffer_atr * atr if a.long else sw[-1][2] + s.stop_buffer_atr * atr, 2)
        # move only when it tightens by at least half a point (not every minute as the ATR in the buffer drifts)
        if (cand >= a.stop + TRAIL_MIN_STEP) if a.long else (cand <= a.stop - TRAIL_MIN_STEP):
            old, a.stop = a.stop, cand
            self.ctx.tighten_stop(a.trade_id, cand)
            self._emit("TRAIL", f"#{a.no} continuation stop {old:.2f} -> {cand:.2f} ({s.trend_timeframe_min}m swing "
                                f"{sw[-1][2]:.2f})", setup=a.no, old=old, new=cand)

    async def _enter(self, x: Setup, b: Bar, close_time: datetime) -> None:
        s = self.settings
        if not (combine(close_time.date(), s.entry_start) <= close_time <= combine(close_time.date(), s.entry_end)):
            self._end(x, "EXPIRED", "outside_entry_window")
            return
        spot = self.ctx.ltp(self.spot) or b.close
        atr = x.atr
        stop = round(x.sweep_px - s.stop_buffer_atr * atr if x.long else x.sweep_px + s.stop_buffer_atr * atr, 2)
        risk = (spot - stop) if x.long else (stop - spot)
        if risk <= 0:
            x.state, x.result = "SKIPPED", "price_beyond_stop"
            return
        if s.max_stop_atr and risk > s.max_stop_atr * atr:
            x.state, x.result = "SKIPPED", f"stop_too_wide_{risk / atr:.1f}atr"
            self._emit("SIGNAL_SKIPPED", f"#{x.no} stop {risk:.1f} pts = {risk / atr:.1f} ATR > {s.max_stop_atr:g}",
                       Level.WARNING, setup=x.no)
            return
        sg = 1 if x.long else -1
        x.entry_spot, x.stop = round(spot, 2), stop
        x.target, x.one_r = round(spot + sg * s.reward_risk * risk, 2), round(spot + sg * risk, 2)
        x.regime = regime_of(s, [Candle(start=t.start, timeframe_min=s.trend_timeframe_min, open=t.open, high=t.high,
                                        low=t.low, close=t.close) for t in self.trend_s.bars], x.side) \
            if s.regime_filter != "off" else None
        if blocks(s, x.regime):
            x.state, x.result = "SKIPPED", "sideways"
            self._emit("REGIME_BLOCKED", f"#{x.no} {x.side} skipped: {x.regime['note']}", Level.WARNING,  # type: ignore[index]
                       setup=x.no, regime=x.regime)
            return
        x.state = "ENTERING"
        self._persist()
        if self.ctx.has_open_position():
            x.state, x.result = "REJECTED", "already_in_position"
            return
        opt_exch = DERIVATIVE_EXCHANGE[s.exchange]
        expiry = pick_expiry(self.ctx.option_expiries(s.underlying, opt_exch), close_time.date(), s.expiry)
        if expiry is None:
            x.state, x.result = "REJECTED", "no_expiry_available"
            return
        d = Direction(x.side)
        side = "above" if x.long else "below"
        partial = ([PartialTarget("1R", x.one_r, side, s.partial_lots_at_1r)] if s.partial_lots_at_1r else [])
        trailed = x.continuation and s.cont_exit == "trail_15m"
        if trailed:
            x.target = None  # no target: the trend timeframe's swings trail the stop
        signal = EntrySignal(
            slot_id=x.slot_id, direction=d,
            option=InstrumentKey(underlying=s.underlying, exchange=opt_exch,
                                 instrument_type=InstrumentType.CE if x.long else InstrumentType.PE,
                                 expiry=expiry, strike=atm_strike(spot, s.step)),
            lots=s.lots, spot=spot, trigger=b.close, order_type=OrderType(s.entry_order_type),
            limit_slippage_pct=s.limit_slippage_pct, signal_time=self.ctx.now(),
            exit_plan=ExitPlan(exit_at=combine(close_time.date(), self.ctx.square_off_time()), underlying=self.spot,
                               spot_sl=stop, spot_sl_side="below" if x.long else "above",
                               tp_spot_level=x.target, tp_spot_side=side,
                               protective_sl_pct=s.protective_sl_pct or None, partial_targets=partial),
            meta={"setup_no": x.no, "underlying": s.underlying, "sweep": x.sweep_px, "swept_level": x.level,
                  "swept_kind": x.level_kind, "zone": f"{x.zone_kind} {x.zone_lo}-{x.zone_hi}", "risk_pts": round(risk, 2),
                  "risk_atr": round(risk / atr, 2), "target": x.target, "regime_entry": x.regime},
        )
        self._emit("SETUP", f"ENTRY #{x.no} {x.side}{' continuation' if x.continuation else ''} {x.zone_kind} "
                            f"{x.zone_lo:.2f}-{x.zone_hi:.2f} spot={spot:.2f} SL={stop:.2f} ({risk:.1f} pts, "
                            f"{risk / atr:.1f} ATR) " + (f"target={x.target:.2f}" if x.target is not None else
                                                         f"no target (trailed on {s.trend_timeframe_min}m swings)")
                   + f" +1R={x.one_r:.2f}" + (f" regime={x.regime['note']}" if x.regime else ""), setup=x.no)
        info = await self.ctx.submit_entry(signal)
        if info is None:
            x.state, x.result = "REJECTED", "signal_rejected"
        else:
            x.trade_id, x.entry_time = info.trade_id, self.ctx.now()
            x.state = "IN_TRADE" if info.status == "OPEN" else "DONE"
            if info.status != "OPEN":
                x.result = info.exit_reason or "entry_failed"
            for o in self.setups:  # one position at a time: the other ideas are dropped
                if o is not x and o.state in WAITING:
                    self._end(o, "CANCELLED", "position_taken")
        self._persist()

    # ---- ticks: breakeven at +1R
    async def on_tick(self, tick: Tick) -> None:
        if self.spot is None or tick.token != self.spot.token:
            return
        a = self.active
        if a is None or a.state != "IN_TRADE" or not a.trade_id or a.be_moved or not self.settings.breakeven_at_1r:
            return
        if a.continuation and not self.settings.cont_breakeven:
            return
        if a.one_r is None or a.entry_spot is None:
            return
        if (tick.ltp >= a.one_r) if a.long else (tick.ltp <= a.one_r):
            a.be_moved = True
            lock = self.settings.breakeven_lock_r
            stop = round(a.entry_spot + (a.one_r - a.entry_spot) * lock, 2)
            self.ctx.tighten_stop(a.trade_id, stop)
            what = f"entry + {lock:g}R = {stop:.2f}" if lock else f"entry {stop:.2f}"
            self._emit("BREAKEVEN", f"#{a.no} spot {tick.ltp:.2f} reached +1R: stop moved to {what}", setup=a.no)
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
        return self.active is None and not any(x.state in ("ARMING", "ZONE", "CARMING", "CZONE") for x in self.setups)

    def apply_settings(self, new: StrategySettings) -> None:
        assert isinstance(new, SmcSettings)
        old = self.settings
        self.settings = new
        if (new.trend_timeframe_min, new.entry_timeframe_min, new.swing_strength) != (
                old.trend_timeframe_min, old.entry_timeframe_min, old.swing_strength):
            self.cold, self._warm_retry_at = True, None  # rebuilt from history on the next timer

    def phase(self) -> str:
        if self.active is not None:
            return "IN_TRADE"
        return "WATCHING" if any(x.state in WAITING for x in self.setups) else "WAITING"

    def status(self) -> dict[str, Any]:
        t, hi, lo = self.trend()
        return {"day": self.day.isoformat() if self.day else None,
                "smc": {"trend": t, "range_high": hi, "range_low": lo,
                        "equilibrium": round((hi + lo) / 2, 2) if hi is not None and lo is not None else None,
                        "atr": round(self.entry.atr_value, 2) if self.entry.atr_value else None,
                        "last": fmt_hm(self.entry.bars[-1].start) if self.entry.bars else None,
                        "close": self.entry.bars[-1].close if self.entry.bars else None,
                        "trend_tf": self.settings.trend_timeframe_min, "entry_tf": self.settings.entry_timeframe_min},
                "setups": [x.to_dict() for x in self.setups],
                "cold": self.cold, "pending_settings": self.has_pending_settings}
