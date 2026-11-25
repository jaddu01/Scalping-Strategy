"""Machine Learning: Lorentzian Classification — from machine_learning_lorentzian_classification.pine.

Indicator by jdehorty (MLExtensions / KernelFunctions v2), strategy wrapper by StrategiesForEveryone; ported to this
platform's rules (ATM option buying on the index spot candles, default 5 min, continuous across days like a chart).

ML signal (per completed candle):
  features   f1..f5 (default RSI 14, WT 10/11, CCI 20, ADX 20, RSI 9), each normalised to 0..1 as MLExtensions does
  label      of every past candle: -1 if close 4 candles earlier < its close, +1 if >, 0 if equal (as the script)
  kNN        Lorentzian distance sum(log(1 + |f - f_i|)) to the last `max_bars_back` candles, every 4th skipped;
             the script's nearest-neighbour list (`neighbors_count`, threshold at the 3/4 element) is kept across candles
  prediction sum of the neighbours' labels; signal = sign(prediction) when the volatility / regime / ADX filters pass,
             else the previous signal
  entries    BUY when the signal turns long and the Nadaraya-Watson kernel (rational quadratic, h 8, r 8, x 25) rises;
             SELL when it turns short and the kernel falls
Trade wrapper (the strategy section of the script):
  EMA filter long only above EMA(200), short only below (`ema_filter`)
  stop       close -/+ ATR(14) x 1.5 (`stop_type` atr) or the lowest low / highest high of 10 candles (swing)
  breakeven  once spot reaches entry + `breakeven_rr` x R the stop moves to the entry
  take profit `tp_lots` at `tp_rr` x R (script: 50 % at 3 R); the rest has no target
  close      an opposite signal reverses when the new side is allowed, else closes the trade if it is in profit;
             a supertrend (ATR 9, factor 2.5) cross against the trade closes it if it is in profit
  hard exit  the 15:15 square-off (platform); entries `entry_start`-`entry_end`; one position at a time
Deviation from the Pine script (on purpose): TradingView compares against the first 2000 bars loaded on the chart
(the arrays are never trimmed), so its signals depend on how much history the chart has; here the neighbours come from
the most recent `max_bars_back` candles, which gives the same result on any start date.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from brokers.base import Candle, Exchange, Instrument, InstrumentKey, InstrumentType, OrderType, Tick
from core.clock import combine, fmt_hm
from core.events import Category, Level
from core.indicators import ATR, RMA, RSI
from strategies.base import Direction, EntrySignal, ExitPlan, PartialTarget, Strategy, StrategySettings, TradeInfo
from strategies.nifty_ref_breakout import (BSE_INDICES, DERIVATIVE_EXCHANGE, MARKET_CLOSE, MARKET_OPEN, atm_strike,
                                           pick_expiry)
from strategies.regime_filter import RegimeSettings, blocks, regime_of
from strategies.registry import register
from strategies.smc_playbook import STRIKE_STEP

FeatureKind = Literal["RSI", "WT", "CCI", "ADX"]
KEEP_CANDLES = 400  # completed candles kept for the platform's sideways filter
WARM_RETRY_FIRST, WARM_RETRY_MAX = 20.0, 300.0
PREDICT_WARMUP_TAIL = 80  # warm-up candles that also run the kNN (the rest only build features / labels)


class LorentzianSettings(RegimeSettings):
    FORM_FIRST: ClassVar[tuple[str, ...]] = ("underlying", "exchange", "candle_timeframe_min", "lots", "tp_lots")

    underlying: str = Field("NIFTY", title="Underlying index")
    exchange: Literal["NSE", "BSE"] = Field("NSE", title="Index exchange")
    candle_timeframe_min: int = Field(5, ge=1, le=60, title="Candle timeframe (minutes)")
    # --- Pine "General Settings"
    neighbors_count: int = Field(8, ge=1, le=100, title="Neighbors count")
    max_bars_back: int = Field(2000, ge=100, le=5000, title="Max bars back",
                               description="Candles the kNN compares against (the most recent ones)")
    feature_count: int = Field(5, ge=2, le=5, title="Feature count")
    # --- Pine "Feature Engineering"
    f1_kind: FeatureKind = Field("RSI", title="Feature 1")
    f1_a: int = Field(14, ge=1, le=200, title="Feature 1 parameter A")
    f1_b: int = Field(1, ge=1, le=200, title="Feature 1 parameter B")
    f2_kind: FeatureKind = Field("WT", title="Feature 2")
    f2_a: int = Field(10, ge=1, le=200, title="Feature 2 parameter A")
    f2_b: int = Field(11, ge=1, le=200, title="Feature 2 parameter B")
    f3_kind: FeatureKind = Field("CCI", title="Feature 3")
    f3_a: int = Field(20, ge=1, le=200, title="Feature 3 parameter A")
    f3_b: int = Field(1, ge=1, le=200, title="Feature 3 parameter B")
    f4_kind: FeatureKind = Field("ADX", title="Feature 4")
    f4_a: int = Field(20, ge=1, le=200, title="Feature 4 parameter A")
    f4_b: int = Field(2, ge=1, le=200, title="Feature 4 parameter B")
    f5_kind: FeatureKind = Field("RSI", title="Feature 5")
    f5_a: int = Field(9, ge=1, le=200, title="Feature 5 parameter A")
    f5_b: int = Field(1, ge=1, le=200, title="Feature 5 parameter B")
    # --- Pine "Filters"
    use_volatility_filter: bool = Field(True, title="Volatility filter (ATR 1 > ATR 10)")
    use_regime_filter: bool = Field(True, title="Regime filter")
    regime_threshold: float = Field(-0.1, ge=-10, le=10, title="Regime threshold")
    use_adx_filter: bool = Field(False, title="ADX filter")
    adx_threshold: int = Field(20, ge=0, le=100, title="ADX threshold")
    # --- Pine "Kernel Settings"
    use_kernel_filter: bool = Field(True, title="Trade with kernel")
    use_kernel_smoothing: bool = Field(False, title="Enhance kernel smoothing (crossover)")
    kernel_lookback: int = Field(8, ge=3, le=100, title="Kernel lookback window (h)")
    kernel_weight: float = Field(8.0, gt=0, le=100, title="Kernel relative weighting (r)")
    kernel_level: int = Field(25, ge=2, le=100, title="Kernel regression level (x)")
    kernel_lag: int = Field(2, ge=1, le=10, title="Kernel lag")
    # --- strategy section
    ema_filter: bool = Field(True, title="EMA filter (long above, short below)")
    ema_length: int = Field(200, ge=1, le=500, title="EMA length")
    stop_type: Literal["atr", "swing"] = Field("atr", title="Stop-loss source",
                                               description="atr: close -/+ ATR x multiplier · swing: lowest low / highest "
                                                           "high of the last N candles")
    atr_length: int = Field(14, ge=1, le=100, title="Stop ATR period")
    atr_multiplier: float = Field(1.5, gt=0, le=20, title="Stop ATR multiplier")
    swing_bars: int = Field(10, ge=2, le=100, title="Swing stop: candles")
    use_breakeven: bool = Field(True, title="Move the stop to entry at the breakeven level")
    breakeven_rr: float = Field(1.0, gt=0, le=20, title="Breakeven at (× risk)")
    use_take_profit: bool = Field(True, title="Take profit")
    tp_rr: float = Field(3.0, gt=0, le=50, title="Take profit at (× risk)")
    lots: int = Field(10, ge=1, le=200, title="Lots")
    tp_lots: int = Field(5, ge=0, le=200, title="Lots sold at the take profit",
                         description="Script: 50 % of the position; the rest rides with the stop / closes")
    close_with_supertrend: bool = Field(True, title="Close in profit on a supertrend cross")
    supertrend_atr: int = Field(9, ge=1, le=100, title="Supertrend ATR period")
    supertrend_factor: float = Field(2.5, gt=0, le=20, title="Supertrend factor")
    min_risk_points: float = Field(0.0, ge=0, le=1000, title="Skip if risk below (points, 0 = off)")
    max_risk_points: float = Field(0.0, ge=0, le=1000, title="Skip if risk above (points, 0 = off)")
    # --- platform rules
    allow_long: bool = Field(True, title="Take long trades (buy CE)")
    allow_short: bool = Field(True, title="Take short trades (buy PE)")
    entry_start: time = Field(time(9, 20), title="New entries from (IST)")
    entry_end: time = Field(time(15, 0), title="New entries until (IST)")
    strike_step: int = Field(0, ge=0, le=1000, title="Strike step (0 = automatic for the index)")
    expiry: Literal["nearest_weekly", "next_weekly"] = Field("nearest_weekly", title="Expiry")
    entry_order_type: Literal["MARKET", "LIMIT"] = Field("MARKET", title="Entry order type")
    limit_slippage_pct: float = Field(1.0, ge=0, le=20, title="LIMIT slippage (%)")
    protective_sl_pct: float = Field(0.0, ge=0, le=95, title="Protective SL-M (% of premium, 0 = off)")
    warmup_candles: int = Field(2300, ge=300, le=6000, title="Warm-up candles",
                                description="History loaded at start: Max bars back + the EMA / feature warm-up")

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
    def _checks(self) -> "LorentzianSettings":
        if self.underlying in BSE_INDICES and self.exchange != "BSE":
            raise ValueError(f"{self.underlying} trades on BSE: set Index exchange to BSE")
        if self.exchange == "BSE" and self.underlying not in BSE_INDICES:
            raise ValueError(f"{self.underlying} is not a BSE index: set Index exchange to NSE")
        if self.tp_lots > self.lots:
            raise ValueError("lots sold at the take profit cannot be more than Lots")
        if self.use_take_profit and self.use_breakeven and self.tp_rr <= self.breakeven_rr:
            raise ValueError("take profit must be beyond the breakeven level")
        if self.min_risk_points and self.max_risk_points and self.min_risk_points >= self.max_risk_points:
            raise ValueError("risk filter: 'skip if risk below' must be less than 'skip if risk above'")
        if not (self.allow_long or self.allow_short):
            raise ValueError("allow long trades, short trades, or both")
        if not (MARKET_OPEN <= self.entry_start < self.entry_end <= MARKET_CLOSE):
            raise ValueError("entry window must be inside market hours 09:15-15:30, start before end")
        if self.strike_step == 0 and self.underlying not in STRIKE_STEP:
            raise ValueError(f"no automatic strike step for {self.underlying}: set Strike step")
        if self.warmup_candles < self.max_bars_back:
            raise ValueError("warm-up candles must be at least Max bars back")
        return self

    @property
    def step(self) -> int:
        return self.strike_step or STRIKE_STEP[self.underlying]

    def features(self) -> list[tuple[str, int, int]]:
        return [(self.f1_kind, self.f1_a, self.f1_b), (self.f2_kind, self.f2_a, self.f2_b),
                (self.f3_kind, self.f3_a, self.f3_b), (self.f4_kind, self.f4_a, self.f4_b),
                (self.f5_kind, self.f5_a, self.f5_b)][:self.feature_count]


# ------------------------------------------------------------------ Pine building blocks (streaming, unit tested)
class PineEMA:
    """ta.ema: seeded with the first value (Pine's na(sum[1]) ? src : ...)."""

    def __init__(self, length: int) -> None:
        self.alpha = 2.0 / (length + 1)
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class SMA:
    def __init__(self, length: int) -> None:
        self.length, self.window = length, []  # type: ignore[var-annotated]

    def update(self, x: float) -> float | None:
        self.window = (self.window + [x])[-self.length:]
        return sum(self.window) / self.length if len(self.window) == self.length else None


class Normalize:
    """MLExtensions.normalize: min-max over the whole history seen so far, to 0..1."""

    def __init__(self) -> None:
        self.lo, self.hi = 10e10, -10e10

    def update(self, x: float) -> float:
        self.lo, self.hi = min(x, self.lo), max(x, self.hi)
        return (x - self.lo) / max(self.hi - self.lo, 10e-10)


class CCI:
    """ta.cci: (src - sma) / (0.015 x mean absolute deviation)."""

    def __init__(self, length: int) -> None:
        self.length, self.window = length, []  # type: ignore[var-annotated]

    def update(self, x: float) -> float | None:
        self.window = (self.window + [x])[-self.length:]
        if len(self.window) < self.length:
            return None
        m = sum(self.window) / self.length
        dev = sum(abs(v - m) for v in self.window) / self.length
        return 0.0 if dev == 0 else (x - m) / (0.015 * dev)


class WilderADX:
    """The ADX of MLExtensions (n_adx / filter_adx): Wilder sums of TR / DM, then ta.rma of DX."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.tr = self.plus = self.minus = 0.0
        self.prev: tuple[float, float, float] | None = None
        self.rma = RMA(length)

    def update(self, high: float, low: float, close: float) -> float | None:
        ph, pl, pc = self.prev if self.prev else (0.0, 0.0, 0.0)  # nz(x[1]) = 0 on the first candle, as in Pine
        self.prev = (high, low, close)
        tr = max(high - low, abs(high - pc), abs(low - pc))
        up, down = high - ph, pl - low
        dm_plus = max(up, 0.0) if up > down else 0.0
        dm_minus = max(down, 0.0) if down > up else 0.0
        n = self.length
        self.tr = self.tr - self.tr / n + tr
        self.plus = self.plus - self.plus / n + dm_plus
        self.minus = self.minus - self.minus / n + dm_minus
        if self.tr == 0:
            return None
        di_p, di_m = self.plus / self.tr * 100, self.minus / self.tr * 100
        dx = 0.0 if di_p + di_m == 0 else abs(di_p - di_m) / (di_p + di_m) * 100
        return self.rma.update(dx)


class Feature:
    """One ML feature (MLExtensions n_rsi / n_wt / n_cci / n_adx), 0..1."""

    def __init__(self, kind: str, a: int, b: int) -> None:
        self.kind = kind
        if kind == "RSI":
            self.rsi, self.ema = RSI(a), PineEMA(b)
        elif kind == "WT":
            self.ema1, self.ema2, self.ema3, self.sma, self.norm = PineEMA(a), PineEMA(a), PineEMA(b), SMA(4), Normalize()
        elif kind == "CCI":
            self.cci, self.ema, self.norm = CCI(a), PineEMA(b), Normalize()
        else:
            self.adx = WilderADX(a)

    def update(self, c: Candle) -> float | None:
        if self.kind == "RSI":
            r = self.rsi.update(c.close)
            return None if r is None else self.ema.update(r) / 100.0  # rescale(ema(rsi), 0, 100, 0, 1)
        if self.kind == "WT":
            src = (c.high + c.low + c.close) / 3
            e1 = self.ema1.update(src)
            e2 = self.ema2.update(abs(src - e1))
            ci = 0.0 if e2 == 0 else (src - e1) / (0.015 * e2)
            wt1 = self.ema3.update(ci)
            wt2 = self.sma.update(wt1)
            return None if wt2 is None else self.norm.update(wt1 - wt2)
        if self.kind == "CCI":
            v = self.cci.update(c.close)
            return None if v is None else self.norm.update(self.ema.update(v))
        a = self.adx.update(c.high, c.low, c.close)
        return None if a is None else a / 100.0


class RegimeFilter:
    """MLExtensions.regime_filter on ohlc4: Kalman-like line, |slope| vs its EMA(200)."""

    def __init__(self) -> None:
        self.v1 = self.v2 = 0.0
        self.klmf: float | None = None
        self.prev_src: float | None = None
        self.ema = PineEMA(200)

    def update(self, c: Candle) -> float | None:
        src = (c.open + c.high + c.low + c.close) / 4
        if self.prev_src is None:
            self.prev_src, self.klmf = src, src
            return None
        self.v1 = 0.2 * (src - self.prev_src) + 0.8 * self.v1
        self.v2 = 0.1 * (c.high - c.low) + 0.8 * self.v2
        self.prev_src = src
        omega = abs(self.v1 / self.v2) if self.v2 else 0.0
        alpha = (-omega ** 2 + math.sqrt(omega ** 4 + 16 * omega ** 2)) / 8
        prev = self.klmf if self.klmf is not None else src
        self.klmf = alpha * src + (1 - alpha) * prev
        slope = abs(self.klmf - prev)
        avg = self.ema.update(slope)
        return (slope - avg) / avg if avg else 0.0


class Supertrend:
    """ta.supertrend(factor, atrPeriod) on hl2; direction -1 = up trend (line below), 1 = down trend."""

    def __init__(self, period: int, factor: float) -> None:
        self.atr, self.factor = ATR(period), factor
        self.lower = self.upper = self.value = None  # type: ignore[assignment]
        self.dir = 1
        self.prev_close: float | None = None
        self.prev_atr: float | None = None

    def update(self, c: Candle) -> float | None:
        atr = self.atr.update(c.high, c.low, c.close)
        if atr is None:
            self.prev_close = c.close
            return None
        src = (c.high + c.low) / 2
        lower, upper = src - self.factor * atr, src + self.factor * atr
        pl, pu = self.lower or 0.0, self.upper or 0.0
        pc = self.prev_close if self.prev_close is not None else c.close
        lower = lower if (lower > pl or pc < pl) else pl
        upper = upper if (upper < pu or pc > pu) else pu
        if self.prev_atr is None:
            d = 1
        elif self.value == self.upper:
            d = -1 if c.close > upper else 1
        else:
            d = 1 if c.close < lower else -1
        self.lower, self.upper, self.dir = lower, upper, d
        self.value = lower if d == -1 else upper
        self.prev_close, self.prev_atr = c.close, atr
        return self.value


def kernel_rq(closes: list[float], h: int, r: float, x: int) -> float | None:
    """KernelFunctions.rationalQuadratic(close, h, r, x): weights over close[0] .. close[x + 1]."""
    n = x + 2
    if len(closes) < n:
        return None
    num = den = 0.0
    for i in range(n):
        w = (1 + (i * i) / (h * h * 2 * r)) ** -r
        num += closes[-1 - i] * w
        den += w
    return num / den


def kernel_gauss(closes: list[float], h: int, x: int) -> float | None:
    """KernelFunctions.gaussian(close, h, x)."""
    n = x + 2
    if len(closes) < n:
        return None
    num = den = 0.0
    for i in range(n):
        w = math.exp(-(i * i) / (2 * h * h))
        num += closes[-1 - i] * w
        den += w
    return num / den


class LorentzianModel:
    """The indicator, one completed candle at a time; update() returns "buy", "sell" or None."""

    def __init__(self, s: LorentzianSettings) -> None:
        self.s = s
        self.features = [Feature(k, a, b) for k, a, b in s.features()]
        self.rows: list[tuple[float, ...]] = []  # feature rows (aligned with labels)
        self.labels: list[int] = []
        self.closes: list[float] = []
        self.dist: list[float] = []  # the script's `var` neighbour lists, kept across candles
        self.pred: list[int] = []
        self.prediction = 0
        self.signal = 0
        self.prev_signal = 0
        self.atr1, self.atr10 = ATR(1), ATR(10)
        self.regime = RegimeFilter()
        self.adx = WilderADX(14)
        self.ema = PineEMA(s.ema_length)
        self.stop_atr = ATR(s.atr_length)
        self.st = Supertrend(s.supertrend_atr, s.supertrend_factor)
        self.yhat1: list[float] = []
        self.yhat2: list[float] = []
        self.candles: list[Candle] = []
        self.last: Candle | None = None
        self.prev_close: float | None = None
        self.prev_st: float | None = None
        self.st_cross_down = self.st_cross_up = False
        self.ema_value: float | None = None
        self.atr_value: float | None = None
        self.kernel_bull: bool | None = None
        self.n = 0

    def ready(self) -> bool:
        return len(self.rows) >= 100 and self.ema_value is not None and self.atr_value is not None

    def update(self, c: Candle, predict: bool = True) -> str | None:
        s = self.s
        self.n += 1
        self.last = c
        self.candles.append(c)
        if len(self.candles) > KEEP_CANDLES + 100:
            del self.candles[:-KEEP_CANDLES]
        self.closes.append(c.close)
        if len(self.closes) > 200:
            del self.closes[:-100]
        # trade-side series
        self.ema_value = self.ema.update(c.close)
        self.atr_value = self.stop_atr.update(c.high, c.low, c.close)
        st_prev, close_prev = self.prev_st, self.prev_close
        st = self.st.update(c)
        self.st_cross_down = bool(st is not None and st_prev is not None and close_prev is not None
                                  and c.close < st and close_prev >= st_prev)
        self.st_cross_up = bool(st is not None and st_prev is not None and close_prev is not None
                                and c.close > st and close_prev <= st_prev)
        self.prev_st, self.prev_close = st, c.close
        # filters
        a1 = self.atr1.update(c.high, c.low, c.close)
        a10 = self.atr10.update(c.high, c.low, c.close)
        vol_ok = (not s.use_volatility_filter) or (a1 is not None and a10 is not None and a1 > a10)
        slope = self.regime.update(c)
        regime_ok = (not s.use_regime_filter) or (slope is not None and slope >= s.regime_threshold)
        adx = self.adx.update(c.high, c.low, c.close)
        adx_ok = (not s.use_adx_filter) or (adx is not None and adx > s.adx_threshold)
        # kernel
        y1 = kernel_rq(self.closes, s.kernel_lookback, s.kernel_weight, s.kernel_level)
        y2 = kernel_gauss(self.closes, s.kernel_lookback - s.kernel_lag, s.kernel_level)
        if y1 is not None:
            self.yhat1 = (self.yhat1 + [y1])[-3:]
        if y2 is not None:
            self.yhat2 = (self.yhat2 + [y2])[-3:]
        bullish = bearish = not s.use_kernel_filter
        if s.use_kernel_filter and len(self.yhat1) >= 2:
            if s.use_kernel_smoothing and self.yhat2:
                bullish, bearish = self.yhat2[-1] >= self.yhat1[-1], self.yhat2[-1] <= self.yhat1[-1]
            else:
                bullish, bearish = self.yhat1[-2] < self.yhat1[-1], self.yhat1[-2] > self.yhat1[-1]
        self.kernel_bull = bullish if s.use_kernel_filter else None
        # features + label (label of this candle: close 4 candles back vs this close)
        vals = [f.update(c) for f in self.features]
        if any(v is None for v in vals):
            return None
        row = tuple(vals)  # type: ignore[arg-type]
        back = self.closes[-5] if len(self.closes) >= 5 else None
        label = 0 if back is None else (-1 if back < c.close else 1 if back > c.close else 0)
        self.rows.append(row)
        self.labels.append(label)
        keep = s.max_bars_back
        if len(self.rows) > keep + 500:
            del self.rows[:-keep]
            del self.labels[:-keep]
        if predict:
            self._predict(row)
        self.prev_signal = self.signal
        filt = vol_ok and regime_ok and adx_ok
        if self.prediction > 0 and filt:
            self.signal = 1
        elif self.prediction < 0 and filt:
            self.signal = -1
        changed = self.signal != self.prev_signal
        if not predict or not changed:
            return None
        if self.signal == 1 and bullish:
            return "buy"
        if self.signal == -1 and bearish:
            return "sell"
        return None

    def _predict(self, row: tuple[float, ...]) -> None:
        s = self.s
        rows, labels = self.rows[-s.max_bars_back:], self.labels[-s.max_bars_back:]
        k = s.neighbors_count
        cut = int(math.floor(k * 3 / 4 + 0.5))  # Pine math.round
        last = -1.0
        log1p = math.log1p
        for i, (r, y) in enumerate(zip(rows, labels)):
            if i % 4 == 0:
                continue
            d = 0.0
            for a, b in zip(row, r):
                d += log1p(abs(a - b))
            if d >= last:
                last = d
                self.dist.append(d)
                self.pred.append(y)
                if len(self.pred) > k:
                    last = self.dist[cut]
                    self.dist.pop(0)
                    self.pred.pop(0)
        self.prediction = sum(self.pred)


@dataclass
class MLSignal:
    no: int
    side: str  # CE_BUY | PE_BUY
    candle_start: datetime
    entry: float
    stop: float
    risk: float
    be_level: float
    tp1: float | None
    prediction: int = 0
    state: str = "NEW"  # NEW | ENTERING | IN_TRADE | DONE | SKIPPED | REJECTED
    result: str | None = None
    trade_id: str | None = None
    tp_hit: str = ""
    be_moved: bool = False
    regime: dict[str, Any] | None = None

    @property
    def long(self) -> bool:
        return self.side == "CE_BUY"

    @property
    def slot_id(self) -> str:
        return f"{self.candle_start.date().isoformat()}#L{self.no}"

    def to_state(self) -> dict[str, Any]:
        d = asdict(self)
        d["candle_start"] = self.candle_start.isoformat()
        return d

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "MLSignal":
        d = dict(d)
        d["candle_start"] = datetime.fromisoformat(d["candle_start"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self, tf: int) -> dict[str, Any]:
        return {"no": self.no, "time": fmt_hm(self.candle_start + timedelta(minutes=tf)), "direction": self.side,
                "entry": self.entry, "stop": self.stop, "risk": round(self.risk, 2), "tp1": self.tp1,
                "tp_hit": self.tp_hit, "be": self.be_moved, "state": self.state, "result": self.result,
                "trade_id": self.trade_id, "prediction": self.prediction,
                "regime": (self.regime or {}).get("note"), "regime_block": bool((self.regime or {}).get("would_block"))}


@register
class LorentzianML(Strategy):
    template_id: ClassVar[str] = "lorentzian_ml"
    display_name: ClassVar[str] = "Machine Learning: Lorentzian Classification (Pine, any index)"
    description: ClassVar[str] = ("machine_learning_lorentzian_classification.pine: kNN (Lorentzian distance) on 5 "
                                  "features + kernel filter; buy the ATM CE / PE above / below EMA 200, ATR stop, "
                                  "breakeven at 1R, half at 3R, supertrend / opposite-signal close in profit.")
    version: ClassVar[str] = "1.0"
    settings_model: ClassVar[type[StrategySettings]] = LorentzianSettings
    warmup_days: ClassVar[int] = 32  # backtests load this many earlier trading days (Max bars back on 5-min candles)

    def __init__(self, settings: LorentzianSettings, ctx: Any) -> None:
        super().__init__(settings, ctx)
        self.settings: LorentzianSettings = settings
        self.model = LorentzianModel(settings)
        self.spot: Instrument | None = None
        self.day: date | None = None
        self.signals: list[MLSignal] = []
        self.last_start: datetime | None = None
        self.cold = False
        self._warm_retry_at: datetime | None = None
        self._warm_wait = WARM_RETRY_FIRST
        self._saved: Any = None
        self._pending: tuple[MLSignal, datetime] | None = None

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
    def active(self) -> MLSignal | None:
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
        self.signals = [MLSignal.from_state(x) for x in data.get("signals") or []]
        for x in self.signals:
            if x.state in ("ENTERING", "NEW"):
                x.state, x.result = "REJECTED", "interrupted_entry"

    # ---- lifecycle
    async def on_start(self) -> None:
        self.spot = self.ctx.resolve(self.spot_key)
        now = self.ctx.now()
        self.day = self.display_day(now.date())  # a weekend / holiday shows the last trading day
        self._load(self.day)
        await self._try_warm_up(now, first=True)

    async def _try_warm_up(self, now: datetime, first: bool = False) -> bool:
        s = self.settings
        try:
            hist = await self.ctx.history(self.spot, s.candle_timeframe_min, s.warmup_candles)
        except Exception:
            hist = []
        if len(hist) >= 400:
            model = LorentzianModel(s)
            tail = len(hist) - PREDICT_WARMUP_TAIL
            for i, c in enumerate(hist):
                model.update(c, predict=i >= tail)
            if model.ready():
                self.model, self.last_start = model, hist[-1].start
                short = len(hist) < s.max_bars_back
                self._emit("WARMUP", f"history: {len(hist)} candles ({len(model.rows)} feature rows); prediction="
                                     f"{model.prediction:+d} signal={model.signal:+d} EMA={model.ema_value:.2f}"
                                     + (f"; fewer than Max bars back ({s.max_bars_back})" if short else "")
                                     + ("; signals resume" if self.cold else ""),
                           Level.WARNING if (self.cold or short) else Level.DEBUG)
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
        if self.day != now.date():  # new day: the model continues (like the chart), the signal list restarts
            self.day, self.signals, self._saved, self._pending = now.date(), [], None, None
        if self.cold and self.spot is not None and (self._warm_retry_at is None or now >= self._warm_retry_at):
            await self._try_warm_up(now)
        await self._maybe_take_pending(now)
        self.maybe_apply_pending()
        self._persist()

    # ---- candles
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
        sig = self.model.update(candle)
        self.last_start = candle.start
        if self.cold:
            return
        m = self.model
        a = self.active
        close_time = candle.start + timedelta(minutes=s.candle_timeframe_min)
        if sig is not None:
            self._emit("ML_SIGNAL", f"{sig.upper()} {fmt_hm(candle.start)} close={candle.close:.2f} prediction="
                                    f"{m.prediction:+d} EMA={m.ema_value:.2f}")
        # exits of the open trade (the script closes only in profit, or reverses into an allowed new trade)
        if a is not None and a.state == "IN_TRADE" and a.trade_id:
            in_profit = (candle.close > a.entry) if a.long else (candle.close < a.entry)
            opposite = (sig == "sell") if a.long else (sig == "buy")
            st_against = m.st_cross_down if a.long else m.st_cross_up
            reverse_ok = opposite and self._side_allowed(not a.long, candle.close)
            if opposite and (reverse_ok or in_profit):
                self._emit("REVERSE_EXIT" if reverse_ok else "SIGNAL_EXIT",
                           f"#{a.no} {a.side}: opposite signal{' (reversing)' if reverse_ok else ', in profit'}",
                           setup=a.no)
                await self.ctx.request_exit(a.trade_id, "opposite_signal")
            elif s.close_with_supertrend and st_against and in_profit:
                self._emit("SUPERTREND_EXIT", f"#{a.no} {a.side}: close crossed the supertrend, in profit", setup=a.no)
                await self.ctx.request_exit(a.trade_id, "supertrend")
        if sig is not None:
            x = self._new_signal(candle, sig == "buy", close_time)
            if x is not None:
                if self.active is None and not self.ctx.has_open_position():
                    await self._enter(x)
                else:
                    self._pending = (x, close_time + timedelta(minutes=s.candle_timeframe_min))
        self._persist()

    def _side_allowed(self, long: bool, close: float) -> bool:
        s, ema = self.settings, self.model.ema_value
        if not (s.allow_long if long else s.allow_short):
            return False
        return not s.ema_filter or ema is None or (close > ema if long else close < ema)

    def _new_signal(self, c: Candle, long: bool, close_time: datetime) -> MLSignal | None:
        s, m = self.settings, self.model
        if s.stop_type == "swing":
            recent = m.candles[-s.swing_bars:]
            stop = min(x.low for x in recent) if long else max(x.high for x in recent)
        else:
            stop = c.close - m.atr_value * s.atr_multiplier if long else c.close + m.atr_value * s.atr_multiplier  # type: ignore[operator]
        stop = round(stop, 2)
        risk = round((c.close - stop) if long else (stop - c.close), 2)
        sg = 1 if long else -1
        x = MLSignal(no=len(self.signals) + 1, side="CE_BUY" if long else "PE_BUY", candle_start=c.start,
                     entry=round(c.close, 2), stop=stop, risk=risk,
                     be_level=round(c.close + sg * s.breakeven_rr * risk, 2),
                     tp1=round(c.close + sg * s.tp_rr * risk, 2) if s.use_take_profit and s.tp_lots else None,
                     prediction=m.prediction)
        self.signals.append(x)
        reason = None
        if not (s.allow_long if long else s.allow_short):
            reason = "direction_off"
        elif s.ema_filter and m.ema_value is not None and not (c.close > m.ema_value if long else c.close < m.ema_value):
            reason = "ema_filter"
        elif not (combine(close_time.date(), s.entry_start) <= close_time <= combine(close_time.date(), s.entry_end)):
            reason = "outside_entry_window"
        elif risk <= 0:
            reason = "no_risk"
        elif s.min_risk_points and risk < s.min_risk_points:
            reason = "risk_below_min"
        elif s.max_risk_points and risk > s.max_risk_points:
            reason = "risk_above_max"
        if reason:
            x.state, x.result = "SKIPPED", reason
            return None
        x.regime = regime_of(s, m.candles, x.side) if s.regime_filter != "off" else None
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

    async def _enter(self, x: MLSignal) -> None:
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
        partials = [PartialTarget("tp1", x.tp1, side, s.tp_lots)] if x.tp1 is not None and s.tp_lots < s.lots else []
        final = x.tp1 if x.tp1 is not None and s.tp_lots >= s.lots else None  # TP lots = all lots: TP closes all
        signal = EntrySignal(
            slot_id=x.slot_id, direction=Direction(x.side),
            option=InstrumentKey(underlying=s.underlying, exchange=opt_exch,
                                 instrument_type=InstrumentType.CE if x.long else InstrumentType.PE,
                                 expiry=expiry, strike=atm_strike(spot, s.step)),
            lots=s.lots, spot=spot, trigger=x.entry, order_type=OrderType(s.entry_order_type),
            limit_slippage_pct=s.limit_slippage_pct, signal_time=now,
            exit_plan=ExitPlan(exit_at=combine(now.date(), self.ctx.square_off_time()), underlying=self.spot,
                               spot_sl=x.stop, spot_sl_side="below" if x.long else "above",
                               tp_spot_level=final, tp_spot_side=side,
                               protective_sl_pct=s.protective_sl_pct or None, partial_targets=partials),
            meta={"setup_no": x.no, "underlying": s.underlying, "candle": fmt_hm(x.candle_start), "risk_pts": x.risk,
                  "prediction": x.prediction, "be_level": x.be_level, "tp1": x.tp1, "regime_entry": x.regime},
        )
        self._emit("SETUP", f"ENTRY #{x.no} {x.side} close={x.entry:.2f} prediction={x.prediction:+d} SL={x.stop:.2f} "
                            f"(risk {x.risk:.2f}) BE@{x.be_level:.2f}" + (f" TP={x.tp1:.2f}" if x.tp1 else ""), setup=x.no)
        info = await self.ctx.submit_entry(signal)
        if info is None:
            x.state, x.result = "REJECTED", "signal_rejected"
        else:
            x.trade_id = info.trade_id
            x.state = "IN_TRADE" if info.status == "OPEN" else "DONE"
            if info.status != "OPEN":
                x.result = info.exit_reason or "entry_failed"
        self._persist()

    # ---- ticks: TP reached (table) and the breakeven move
    async def on_tick(self, tick: Tick) -> None:
        if self.spot is None or tick.token != self.spot.token:
            return
        a = self.active
        if a is None or a.state != "IN_TRADE" or not a.trade_id:
            return
        p = tick.ltp
        if a.tp1 is not None and not a.tp_hit and (p >= a.tp1 if a.long else p <= a.tp1):
            a.tp_hit = "tp1"
            self._persist()
        if self.settings.use_breakeven and not a.be_moved and (p > a.be_level if a.long else p < a.be_level):
            a.be_moved = True
            self.ctx.tighten_stop(a.trade_id, a.entry)
            self._emit("BREAKEVEN", f"#{a.no} {self.settings.breakeven_rr:g}R reached: stop moved to entry "
                                    f"{a.entry:.2f}", setup=a.no)
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
        assert isinstance(new, LorentzianSettings)
        self.settings = new
        self.model = LorentzianModel(new)  # any parameter can change the model: rebuild from history
        self.cold, self._warm_retry_at, self.last_start = True, None, None

    def phase(self) -> str:
        return "IN_TRADE" if self.active is not None else "WAITING"

    def status(self) -> dict[str, Any]:
        m = self.model
        tf = self.settings.candle_timeframe_min
        return {"day": self.day.isoformat() if self.day else None,
                "ml": {"prediction": m.prediction, "signal": m.signal, "kernel_bull": m.kernel_bull,
                       "ema": round(m.ema_value, 2) if m.ema_value is not None else None,
                       "supertrend": round(m.st.value, 2) if m.st.value is not None else None,
                       "candle": fmt_hm(m.last.start) if m.last else None,
                       "close": m.last.close if m.last else None, "tf": tf, "rows": len(m.rows)},
                "ce_signals": [x.to_dict(tf) for x in self.signals],
                "use_stop_loss": True, "use_targets": self.settings.use_take_profit,
                "tp_labels": ["tp1"] if self.settings.use_take_profit else [],
                "cold": self.cold, "pending_settings": self.has_pending_settings}
