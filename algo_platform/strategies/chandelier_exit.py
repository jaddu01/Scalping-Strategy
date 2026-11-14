"""Chandelier Exit + Multi-TP — from chandelier_exit_multi_tp.pine (TradingView), on this platform's rules.

Pine logic on the index spot candles (default 5 min, continuous across days like a chart):
  atr        = multiplier x ATR(length)                               (ta.atr: RMA of the true range)
  long stop  = highest(close, length) - atr, ratcheting up while the previous close stays above it
  short stop = lowest(close, length) + atr, ratcheting down while the previous close stays below it
  direction  = long when a close is above the previous short stop, short when below the previous long stop
  BUY signal = direction flips to long  -> BUY the ATM CE at the candle's close
  SELL signal = direction flips to short -> BUY the ATM PE
Exits (spot levels, watched on every tick by the engine):
  stop       the previous candle's low (long) / high (short), +- `sl_buffer_points`; risk R = |entry - stop|
  TP1 / TP2  sell `tp1_lots` at `tp1_rr` x R, `tp2_lots` at `tp2_rr` x R
  TP3        the remaining lots at `tp3_rr` x R
  optional   stop to the entry price once TP1 is reached (`move_sl_to_entry_after_tp1`)
  opposite   an opposite signal closes what is left and opens the new direction once flat (`exit_on_opposite`)
  hard exit  the 15:15 square-off
Platform additions: new entries `entry_start`-`entry_end`; `skip_first_candle` ignores signals of the day's first
candle (its "previous candle" is yesterday's last one, so the stop can be 100+ points away: in a 45-day NIFTY study
those 25 trades averaged a 117-point stop and lost); one position at a time; sideways filter (log-only by default).
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
from strategies.smc_playbook import STRIKE_STEP

KEEP_CANDLES = 400
WARM_RETRY_FIRST, WARM_RETRY_MAX = 20.0, 300.0


class ChandelierSettings(RegimeSettings):
    FORM_FIRST: ClassVar[tuple[str, ...]] = ("underlying", "exchange", "candle_timeframe_min", "tp1_lots", "tp2_lots",
                                            "tp3_lots")

    underlying: str = Field("NIFTY", title="Underlying index",
                            description="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY (NSE) · SENSEX, BANKEX (BSE). "
                                        "A 57-day study lost on BANKNIFTY; NIFTY and SENSEX were positive")
    exchange: Literal["NSE", "BSE"] = Field("NSE", title="Index exchange",
                                            description="NSE: options on NFO · BSE (SENSEX, BANKEX): options on BFO")
    candle_timeframe_min: int = Field(5, ge=1, le=60, title="Candle timeframe (minutes)")
    # --- Pine "Calculation"
    atr_length: int = Field(7, ge=1, le=100, title="ATR period")
    atr_multiplier: float = Field(2.0, gt=0, le=20, title="ATR multiplier")
    use_close: bool = Field(True, title="Use close price for extremums",
                            description="Off: highest high / lowest low instead of highest / lowest close")
    # --- Pine "Targets & Lots"
    tp1_rr: float = Field(2.0, ge=0.5, le=20, title="TP1 (× risk)")
    tp1_lots: int = Field(4, ge=0, le=200, title="TP1 lots")
    tp2_rr: float = Field(3.0, ge=0.5, le=20, title="TP2 (× risk)")
    tp2_lots: int = Field(4, ge=0, le=200, title="TP2 lots")
    tp3_rr: float = Field(4.0, ge=0.5, le=20, title="TP3 (× risk)")
    tp3_lots: int = Field(2, ge=0, le=200, title="TP3 lots",
                          description="Lots per trade = TP1 + TP2 + TP3 lots (4 + 4 + 2 = 10)")
    # --- Pine "Stop Loss"
    sl_buffer_points: float = Field(0.0, ge=0, le=500, title="SL buffer beyond the previous candle (points)")
    move_sl_to_entry_after_tp1: bool = Field(False, title="Move SL to entry after TP1 is hit")
    exit_on_opposite: bool = Field(True, title="Close remaining lots on the opposite signal",
                                   description="…and take the new direction as soon as the exit has filled")
    # --- platform rules
    skip_first_candle: bool = Field(True, title="Skip signals of the day's first candle",
                                    description="Its stop is yesterday's last candle: often 100+ points away after a gap")
    allow_long: bool = Field(True, title="Take long trades (buy CE)")
    allow_short: bool = Field(True, title="Take short trades (buy PE)")
    entry_start: time = Field(time(9, 20), title="New entries from (IST)")
    entry_end: time = Field(time(15, 0), title="New entries until (IST)",
                            description="Open trades are closed by the final square-off (15:15)")
    strike_step: int = Field(0, ge=0, le=1000, title="Strike step (0 = automatic for the index)")
    expiry: Literal["nearest_weekly", "next_weekly"] = Field(
        "nearest_weekly", title="Expiry", description="Nearest listed expiry (monthly-only indices use the monthly)")
    entry_order_type: Literal["MARKET", "LIMIT"] = Field("MARKET", title="Entry order type")
    limit_slippage_pct: float = Field(1.0, ge=0, le=20, title="LIMIT slippage (%)")
    protective_sl_pct: float = Field(0.0, ge=0, le=95, title="Protective SL-M (% of premium, 0 = off)")
    warmup_candles: int = Field(300, ge=50, le=2000, title="Warm-up candles",
                                description="History loaded at start so the Chandelier lines match the chart")

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
    def _checks(self) -> "ChandelierSettings":
        if self.underlying in BSE_INDICES and self.exchange != "BSE":
            raise ValueError(f"{self.underlying} trades on BSE: set Index exchange to BSE")
        if self.exchange == "BSE" and self.underlying not in BSE_INDICES:
            raise ValueError(f"{self.underlying} is not a BSE index: set Index exchange to NSE")
        if not (self.tp1_rr < self.tp2_rr < self.tp3_rr):
            raise ValueError("targets must increase: TP1 < TP2 < TP3")
        if self.lots <= 0:
            raise ValueError("TP1 + TP2 + TP3 lots must be more than 0")
        if not (self.allow_long or self.allow_short):
            raise ValueError("allow long trades, short trades, or both")
        if not (MARKET_OPEN <= self.entry_start < self.entry_end <= MARKET_CLOSE):
            raise ValueError("entry window must be inside market hours 09:15-15:30, start before end")
        if self.strike_step == 0 and self.underlying not in STRIKE_STEP:
            raise ValueError(f"no automatic strike step for {self.underlying}: set Strike step")
        return self

    @property
    def lots(self) -> int:
        return self.tp1_lots + self.tp2_lots + self.tp3_lots

    @property
    def step(self) -> int:
        return self.strike_step or STRIKE_STEP[self.underlying]


class ChandelierLines:
    """The Pine script's Chandelier Exit, one completed candle at a time."""

    def __init__(self, s: ChandelierSettings) -> None:
        self.s = s
        self.atr = ATR(s.atr_length)
        self.window: list[Candle] = []
        self.long_prev: float | None = None
        self.short_prev: float | None = None
        self.prev_close: float | None = None
        self.dir = 1
        self.long_stop: float | None = None
        self.short_stop: float | None = None
        self.last: Candle | None = None
        self.candles: list[Candle] = []  # completed candles (stop = previous candle, sideways filter)

    def update(self, c: Candle) -> str | None:
        """Add a completed candle; returns "buy", "sell" or None."""
        s = self.s
        atr = self.atr.update(c.high, c.low, c.close)
        self.window = (self.window + [c])[-s.atr_length:]
        self.candles.append(c)
        if len(self.candles) > KEEP_CANDLES + 100:
            del self.candles[:-KEEP_CANDLES]
        self.last = c
        if atr is None or len(self.window) < s.atr_length:
            self.prev_close = c.close
            return None
        a = s.atr_multiplier * atr
        hi = max(x.close for x in self.window) if s.use_close else max(x.high for x in self.window)
        lo = min(x.close for x in self.window) if s.use_close else min(x.low for x in self.window)
        ls, ss = hi - a, lo + a
        lsp = ls if self.long_prev is None else self.long_prev
        ssp = ss if self.short_prev is None else self.short_prev
        if self.prev_close is not None and self.prev_close > lsp:
            ls = max(ls, lsp)
        if self.prev_close is not None and self.prev_close < ssp:
            ss = min(ss, ssp)
        new = 1 if c.close > ssp else -1 if c.close < lsp else self.dir
        sig = "buy" if (new == 1 and self.dir == -1) else "sell" if (new == -1 and self.dir == 1) else None
        self.dir, self.long_prev, self.short_prev, self.prev_close = new, ls, ss, c.close
        self.long_stop, self.short_stop = ls, ss
        return sig

    def ready(self) -> bool:
        return self.long_stop is not None


@dataclass
class Signal:
    no: int
    side: str  # CE_BUY | PE_BUY
    candle_start: datetime
    entry: float  # the signal candle's close (levels are measured from it, like the chart)
    stop: float
    risk: float
    tp1: float
    tp2: float
    tp3: float
    state: str = "NEW"  # NEW | ENTERING | IN_TRADE | DONE | SKIPPED | REJECTED
    result: str | None = None
    trade_id: str | None = None
    tp_hit: str = ""  # "tp1", "tp1+tp2", … (from spot, for the table)
    be_moved: bool = False
    reverse_requested: bool = False
    regime: dict[str, Any] | None = None

    @property
    def long(self) -> bool:
        return self.side == "CE_BUY"

    @property
    def slot_id(self) -> str:
        return f"{self.candle_start.date().isoformat()}#C{self.no}"

    def to_state(self) -> dict[str, Any]:
        d = asdict(self)
        d["candle_start"] = self.candle_start.isoformat()
        return d

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "Signal":
        d = dict(d)
        d["candle_start"] = datetime.fromisoformat(d["candle_start"])
        return cls(**d)

    def to_dict(self, tf: int) -> dict[str, Any]:
        return {"no": self.no, "time": fmt_hm(self.candle_start + timedelta(minutes=tf)), "direction": self.side,
                "entry": self.entry, "stop": self.stop, "risk": round(self.risk, 2), "tp1": self.tp1, "tp2": self.tp2,
                "tp3": self.tp3, "tp_hit": self.tp_hit, "be": self.be_moved, "state": self.state,
                "result": self.result, "trade_id": self.trade_id,
                "regime": (self.regime or {}).get("note"), "regime_block": bool((self.regime or {}).get("would_block"))}


@register
class ChandelierExit(Strategy):
    template_id: ClassVar[str] = "chandelier_exit"
    display_name: ClassVar[str] = "Chandelier Exit + Multi-TP (Pine, any index)"
    description: ClassVar[str] = ("chandelier_exit_multi_tp.pine: buy the ATM CE / PE when the Chandelier Exit flips; "
                                  "SL at the previous candle, TP1 2R (4 lots), TP2 3R (4), TP3 4R (2); the opposite "
                                  "signal closes the rest and reverses.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = ChandelierSettings
    warmup_days: ClassVar[int] = 5

    def __init__(self, settings: ChandelierSettings, ctx: Any) -> None:
        super().__init__(settings, ctx)
        self.settings: ChandelierSettings = settings
        self.lines = ChandelierLines(settings)
        self.spot: Instrument | None = None
        self.day: date | None = None
        self.signals: list[Signal] = []
        self.last_start: datetime | None = None
        self.cold = False
        self._warm_retry_at: datetime | None = None
        self._warm_wait = WARM_RETRY_FIRST
        self._saved: Any = None
        self._pending: tuple[Signal, datetime] | None = None  # reversal waiting for the previous exit to fill

    # ---- basics
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
    def active(self) -> Signal | None:
        return next((x for x in reversed(self.signals) if x.state in ("ENTERING", "IN_TRADE")), None)

    # ---- persistence
    def _persist(self) -> None:
        if self.day is None:
            return
        data = {"signals": [x.to_state() for x in self.signals]}
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
        self.signals = [Signal.from_state(x) for x in data.get("signals") or []]
        for x in self.signals:
            if x.state in ("ENTERING", "NEW"):
                x.state, x.result = "REJECTED", "interrupted_entry"

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
            hist = await self.ctx.history(self.spot, s.candle_timeframe_min, s.warmup_candles)
        except Exception:
            hist = []
        if len(hist) >= max(5 * s.atr_length, 50):
            lines = ChandelierLines(s)
            for c in hist:
                lines.update(c)
            self.lines, self.last_start = lines, hist[-1].start
            self._emit("WARMUP", f"history: {len(hist)} candles; direction={'long' if lines.dir == 1 else 'short'} "
                                 f"long stop={lines.long_stop:.2f} short stop={lines.short_stop:.2f}"
                                 + ("; signals resume" if self.cold else ""),
                       Level.WARNING if self.cold else Level.DEBUG)
            self.cold, self._warm_wait = False, WARM_RETRY_FIRST
            return True
        if first or not self.cold:
            self._emit("WARMUP_EMPTY", f"history not available ({len(hist)} candles): no signals until it loads; "
                                       f"retrying", Level.WARNING)
        self.cold = True
        self._warm_retry_at = now + timedelta(seconds=self._warm_wait)
        self._warm_wait = min(self._warm_wait * 2, WARM_RETRY_MAX)
        return False

    def restore(self, trades: list[TradeInfo]) -> None:
        by_slot = {t.slot_id: t for t in trades}
        for x in self.signals:
            t = by_slot.get(x.slot_id)
            if t is None:
                continue
            x.trade_id = t.trade_id
            if t.status == "OPEN":
                x.state = "IN_TRADE"
            elif x.state != "DONE":
                x.state, x.result = "DONE", t.exit_reason or t.status.lower()

    async def on_timer(self, now: datetime) -> None:
        if self.day != now.date():  # new day: the lines continue (like the chart), the signal list restarts
            self.day, self.signals, self._saved, self._pending = now.date(), [], None, None
        if self.cold and self.spot is not None and (self._warm_retry_at is None or now >= self._warm_retry_at):
            await self._try_warm_up(now)
        await self._maybe_take_pending(now)
        self.maybe_apply_pending()
        self._persist()

    # ---- candles: lines, opposite-signal exit, new entry
    async def on_candle(self, instrument: Instrument, candle: Candle) -> None:
        s = self.settings
        if (self.spot is None or instrument.token != self.spot.token or candle.timeframe_min != s.candle_timeframe_min
                or (self.last_start is not None and candle.start <= self.last_start)):
            return
        if not getattr(candle, "complete", True):
            try:
                full = await self.ctx.get_candle(self.spot, candle.start, s.candle_timeframe_min)
            except Exception:
                full = None
            if full is not None:
                candle = full
            else:
                self._emit("PARTIAL_CANDLE", f"{fmt_hm(candle.start)} candle is incomplete and the broker's is not "
                                             f"available yet; using it as is", Level.WARNING)
        prev = self.lines.last
        sig = self.lines.update(candle)
        self.last_start = candle.start
        if self.cold or sig is None or prev is None:
            self._persist()
            return
        close_time = candle.start + timedelta(minutes=s.candle_timeframe_min)
        long = sig == "buy"
        self._emit("CE_SIGNAL", f"{sig.upper()} {fmt_hm(candle.start)} close={candle.close:.2f} long stop="
                                f"{self.lines.long_stop:.2f} short stop={self.lines.short_stop:.2f}")
        a = self.active
        if a is not None and a.long != long and a.state == "IN_TRADE" and s.exit_on_opposite and a.trade_id:
            a.reverse_requested = True
            self._emit("REVERSE_EXIT", f"#{a.no} {a.side}: opposite signal, closing the remaining lots", setup=a.no)
            await self.ctx.request_exit(a.trade_id, "opposite_signal")
        x = self._new_signal(candle, prev, long, close_time)
        if x is not None:
            if self.active is None and not self.ctx.has_open_position():
                await self._enter(x)
            elif s.exit_on_opposite:
                # wait for the previous trade's exit to fill, then enter (within one candle)
                self._pending = (x, close_time + timedelta(minutes=s.candle_timeframe_min))
            else:
                x.state, x.result = "SKIPPED", "already_in_position"
        self._persist()

    def _new_signal(self, c: Candle, prev: Candle, long: bool, close_time: datetime) -> Signal | None:
        s = self.settings
        stop = round(prev.low - s.sl_buffer_points if long else prev.high + s.sl_buffer_points, 2)
        risk = (c.close - stop) if long else (stop - c.close)
        sg = 1 if long else -1
        x = Signal(no=len(self.signals) + 1, side="CE_BUY" if long else "PE_BUY", candle_start=c.start,
                   entry=round(c.close, 2), stop=stop, risk=round(risk, 2),
                   tp1=round(c.close + sg * s.tp1_rr * risk, 2), tp2=round(c.close + sg * s.tp2_rr * risk, 2),
                   tp3=round(c.close + sg * s.tp3_rr * risk, 2))
        self.signals.append(x)
        day_open = combine(c.start.date(), MARKET_OPEN)
        reason = None
        if not (s.allow_long if long else s.allow_short):
            reason = "direction_off"
        elif s.skip_first_candle and c.start <= day_open:
            reason = "first_candle"
        elif not (combine(close_time.date(), s.entry_start) <= close_time <= combine(close_time.date(), s.entry_end)):
            reason = "outside_entry_window"
        elif risk <= 0:
            reason = "no_risk"  # the close is beyond the previous candle's extreme (as the script skips)
        if reason:
            x.state, x.result = "SKIPPED", reason
            return None
        x.regime = regime_of(s, self.lines.candles, x.side) if s.regime_filter != "off" else None
        if blocks(s, x.regime):
            x.state, x.result = "SKIPPED", "sideways"
            self._emit("REGIME_BLOCKED", f"#{x.no} {x.side} skipped: {x.regime['note']}", Level.WARNING,  # type: ignore[index]
                       setup=x.no, regime=x.regime)
            return None
        return x

    async def _maybe_take_pending(self, now: datetime) -> None:
        if self._pending is None:
            return
        x, deadline = self._pending
        if now > deadline:
            x.state, x.result, self._pending = "SKIPPED", "previous_exit_not_filled", None
            return
        if self.active is None and not self.ctx.has_open_position():
            self._pending = None
            await self._enter(x)

    async def _enter(self, x: Signal) -> None:
        s = self.settings
        x.state = "ENTERING"
        self._persist()
        now = self.ctx.now()
        spot = self.ctx.ltp(self.spot) or x.entry
        if (spot <= x.stop) if x.long else (spot >= x.stop):
            x.state, x.result = "SKIPPED", "price_beyond_stop"
            return
        opt_exch = DERIVATIVE_EXCHANGE[s.exchange]
        expiry = pick_expiry(self.ctx.option_expiries(s.underlying, opt_exch), now.date(), s.expiry)
        if expiry is None:
            x.state, x.result = "REJECTED", "no_expiry_available"
            return
        side = "above" if x.long else "below"
        partials = [PartialTarget(lbl, lvl, side, n) for lbl, lvl, n in
                    (("tp1", x.tp1, s.tp1_lots), ("tp2", x.tp2, s.tp2_lots)) if n > 0]
        signal = EntrySignal(
            slot_id=x.slot_id, direction=Direction(x.side),
            option=InstrumentKey(underlying=s.underlying, exchange=opt_exch,
                                 instrument_type=InstrumentType.CE if x.long else InstrumentType.PE,
                                 expiry=expiry, strike=atm_strike(spot, s.step)),
            lots=s.lots, spot=spot, trigger=x.entry, order_type=OrderType(s.entry_order_type),
            limit_slippage_pct=s.limit_slippage_pct, signal_time=now,
            exit_plan=ExitPlan(exit_at=combine(now.date(), self.ctx.square_off_time()), underlying=self.spot,
                               spot_sl=x.stop, spot_sl_side="below" if x.long else "above",
                               tp_spot_level=x.tp3, tp_spot_side=side,
                               protective_sl_pct=s.protective_sl_pct or None, partial_targets=partials),
            meta={"setup_no": x.no, "underlying": s.underlying, "candle": fmt_hm(x.candle_start), "risk_pts": x.risk,
                  "tp1": x.tp1, "tp2": x.tp2, "tp3": x.tp3, "regime_entry": x.regime},
        )
        self._emit("SETUP", f"ENTRY #{x.no} {x.side} close={x.entry:.2f} SL={x.stop:.2f} (risk {x.risk:.2f}) "
                            f"TP1={x.tp1:.2f} TP2={x.tp2:.2f} TP3={x.tp3:.2f}"
                            + (f" regime={x.regime['note']}" if x.regime else ""), setup=x.no)
        info = await self.ctx.submit_entry(signal)
        if info is None:
            x.state, x.result = "REJECTED", "signal_rejected"
        else:
            x.trade_id = info.trade_id
            x.state = "IN_TRADE" if info.status == "OPEN" else "DONE"
            if info.status != "OPEN":
                x.result = info.exit_reason or "entry_failed"
        self._persist()

    # ---- ticks: which targets spot has reached (table) and stop to entry after TP1
    async def on_tick(self, tick: Tick) -> None:
        if self.spot is None or tick.token != self.spot.token:
            return
        a = self.active
        if a is None or a.state != "IN_TRADE" or not a.trade_id:
            return
        p = tick.ltp
        hit = [lbl for lbl, lvl in (("tp1", a.tp1), ("tp2", a.tp2), ("tp3", a.tp3))
               if (p >= lvl if a.long else p <= lvl)]
        if hit and "+".join(hit) != a.tp_hit and len(hit) > len(a.tp_hit.split("+") if a.tp_hit else []):
            a.tp_hit = "+".join(hit)
            self._persist()
        if self.settings.move_sl_to_entry_after_tp1 and not a.be_moved and "tp1" in hit:
            a.be_moved = True
            self.ctx.tighten_stop(a.trade_id, a.entry)
            self._emit("BREAKEVEN", f"#{a.no} TP1 reached: stop moved to entry {a.entry:.2f}", setup=a.no)
            self._persist()

    async def on_trade_update(self, trade: TradeInfo) -> None:
        for x in self.signals:
            if x.slot_id == trade.slot_id or (x.trade_id and x.trade_id == trade.trade_id):
                x.trade_id = trade.trade_id
                if trade.status == "OPEN":
                    x.state = "IN_TRADE"
                else:
                    x.state, x.result = "DONE", trade.exit_reason or trade.status.lower()
        if trade.status != "OPEN":
            await self._maybe_take_pending(self.ctx.now())
        self.maybe_apply_pending()
        self._persist()

    # ---- settings / UI
    def is_idle(self) -> bool:
        return self.active is None and self._pending is None

    def apply_settings(self, new: StrategySettings) -> None:
        assert isinstance(new, ChandelierSettings)
        old = self.settings
        self.settings = new
        if (new.candle_timeframe_min, new.atr_length, new.atr_multiplier, new.use_close) != (
                old.candle_timeframe_min, old.atr_length, old.atr_multiplier, old.use_close):
            same_tf = new.candle_timeframe_min == old.candle_timeframe_min
            candles = self.lines.candles if same_tf else []
            self.lines = ChandelierLines(new)
            for c in candles:  # same candles, new parameters: the lines are recomputed at once
                self.lines.update(c)
            if not candles:
                self.cold, self._warm_retry_at, self.last_start = True, None, None
        else:
            self.lines.s = new

    def phase(self) -> str:
        if self.active is not None:
            return "IN_TRADE"
        return "WAITING"

    def status(self) -> dict[str, Any]:
        ln = self.lines
        tf = self.settings.candle_timeframe_min
        return {"day": self.day.isoformat() if self.day else None,
                "ce": {"direction": ("long" if ln.dir == 1 else "short") if ln.ready() else None,
                       "long_stop": round(ln.long_stop, 2) if ln.long_stop is not None else None,
                       "short_stop": round(ln.short_stop, 2) if ln.short_stop is not None else None,
                       "candle": fmt_hm(ln.last.start) if ln.last else None,
                       "close": ln.last.close if ln.last else None, "tf": tf},
                "ce_signals": [x.to_dict(tf) for x in self.signals],
                "cold": self.cold, "pending_settings": self.has_pending_settings}
