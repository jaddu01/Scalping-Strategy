"""
Sideways-market (chop) filter for the NIFTY EMA21 + RSI50 breakout engine
(used by strategies/ema_rsi_breakout.py; `summarize` turns a result into what is saved with a signal).

Call check_regime() with COMPLETED 5-minute candles (oldest first) before
placing any entry. If result.sideways is True, skip the signal and log
result.reasons.

Feed at least ~100 candles (include the previous day's candles so the
indicators are warmed up at 9:15).

No external dependencies. Candles can be dicts with keys
open/high/low/close, or any object with those attributes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence


@dataclass
class RegimeConfig:
    ema_len: int = 21
    atr_len: int = 14
    slope_bars: int = 5          # EMA slope measured over N candles
    min_slope_atr: float = 0.3   # |EMA[now] - EMA[N ago]| / ATR must be >= this
    adx_len: int = 14
    min_adx: float = 20.0        # ADX below this = no trend
    chop_len: int = 14
    max_chop: float = 61.8       # Choppiness Index above this = sideways
    cross_lookback: int = 20     # candles to count EMA crosses over
    max_crosses: int = 3         # more crosses than this = sideways
    range_lookback: int = 12     # candles for the recent box (12 = 1 hour)
    min_range_atr: float = 2.5   # box height / ATR below this = sideways
    min_fails: int = 2           # block when at least this many checks fail


@dataclass
class RegimeResult:
    sideways: bool
    fails: int
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


# ---------- candle access ----------

def _get(c: Any, key: str) -> float:
    return float(c[key]) if isinstance(c, dict) else float(getattr(c, key))


# ---------- indicator helpers ----------

def _ema(values: list[float], length: int) -> list[float]:
    out = [math.nan] * len(values)
    if len(values) < length:
        return out
    k = 2 / (length + 1)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rma(values: list[float], length: int, start: int = 0) -> list[float]:
    """Wilder smoothing, seeded with an SMA."""
    out = [math.nan] * len(values)
    if len(values) - start < length:
        return out
    prev = sum(values[start:start + length]) / length
    out[start + length - 1] = prev
    for i in range(start + length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def _true_range(h: list[float], l: list[float], c: list[float]) -> list[float]:
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def _adx(h: list[float], l: list[float], c: list[float], length: int) -> list[float]:
    n = len(c)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = h[i] - h[i - 1]
        down = l[i - 1] - l[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr = _true_range(h, l, c)
    tr_s = _rma(tr, length, 1)
    p_s = _rma(plus_dm, length, 1)
    m_s = _rma(minus_dm, length, 1)
    dx = [math.nan] * n
    first = -1
    for i in range(n):
        if math.isnan(tr_s[i]) or tr_s[i] == 0:
            continue
        pdi = 100 * p_s[i] / tr_s[i]
        mdi = 100 * m_s[i] / tr_s[i]
        s = pdi + mdi
        dx[i] = 0.0 if s == 0 else 100 * abs(pdi - mdi) / s
        if first < 0:
            first = i
    if first < 0:
        return [math.nan] * n
    return _rma(dx, length, first)


def _choppiness(h: list[float], l: list[float], c: list[float], length: int) -> float:
    """Choppiness Index: ~100 = pure range, ~0 = strong trend."""
    n = len(c)
    if n < length + 1:
        return math.nan
    tr = _true_range(h, l, c)
    sum_tr = sum(tr[n - length:])
    rng = max(h[n - length:]) - min(l[n - length:])
    if rng <= 0:
        return 100.0
    return 100 * math.log10(sum_tr / rng) / math.log10(length)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


# ---------- main check ----------

def check_regime(candles: Sequence[Any], cfg: RegimeConfig | None = None) -> RegimeResult:
    cfg = cfg or RegimeConfig()
    need = max(cfg.ema_len + cfg.slope_bars, cfg.adx_len * 3,
               cfg.cross_lookback + cfg.ema_len, cfg.chop_len + 1)
    if len(candles) < need:
        return RegimeResult(
            sideways=True,
            fails=cfg.min_fails,
            reasons=[f"not enough candles ({len(candles)} < {need}) - blocking to be safe"],
        )

    h = [_get(x, "high") for x in candles]
    l = [_get(x, "low") for x in candles]
    c = [_get(x, "close") for x in candles]
    last = len(c) - 1

    ema = _ema(c, cfg.ema_len)
    atr = _rma(_true_range(h, l, c), cfg.atr_len)[last]
    ema_now = ema[last]
    slope_atr = (ema_now - ema[last - cfg.slope_bars]) / atr
    adx_now = _adx(h, l, c, cfg.adx_len)[last]
    chop = _choppiness(h, l, c, cfg.chop_len)

    crosses = 0
    for i in range(last - cfg.cross_lookback + 1, last + 1):
        prev_side = _sign(c[i - 1] - ema[i - 1])
        side = _sign(c[i] - ema[i])
        if prev_side != 0 and side != 0 and side != prev_side:
            crosses += 1

    lo = last - cfg.range_lookback + 1
    range_atr = (max(h[lo:]) - min(l[lo:])) / atr

    reasons: list[str] = []
    if abs(slope_atr) < cfg.min_slope_atr:
        reasons.append(f"flat EMA (slope {slope_atr:.2f} ATR < {cfg.min_slope_atr})")
    if adx_now < cfg.min_adx:
        reasons.append(f"weak trend (ADX {adx_now:.1f} < {cfg.min_adx})")
    if chop > cfg.max_chop:
        reasons.append(f"choppy (CHOP {chop:.1f} > {cfg.max_chop})")
    if crosses > cfg.max_crosses:
        reasons.append(f"{crosses} EMA crosses in last {cfg.cross_lookback} candles")
    if range_atr < cfg.min_range_atr:
        reasons.append(f"tight box (last {cfg.range_lookback} candles = {range_atr:.2f} ATR)")

    return RegimeResult(
        sideways=len(reasons) >= cfg.min_fails,
        fails=len(reasons),
        reasons=reasons,
        metrics={
            "ema": ema_now, "atr": atr, "ema_slope_atr": slope_atr, "adx": adx_now,
            "chop": chop, "crosses": crosses, "range_atr": range_atr,
        },
    )


def direction_allowed(direction: Literal["CE_BUY", "PE_BUY"], r: RegimeResult) -> bool:
    """Even in a trend, only trade WITH the EMA slope."""
    slope = r.metrics.get("ema_slope_atr", 0.0)
    return slope > 0 if direction == "CE_BUY" else slope < 0


def summarize(r: RegimeResult, direction: Literal["CE_BUY", "PE_BUY"], block_against_slope: bool = False,
              mode: str = "log_only") -> dict[str, Any]:
    """What is saved next to a signal: the result, its reasons / metrics, and whether it would block."""
    dir_ok = direction_allowed(direction, r) if r.metrics else False
    would_block = r.sideways or (block_against_slope and not dir_ok)
    reasons = list(r.reasons)
    if not dir_ok and r.metrics:
        reasons.append(f"{direction} against the EMA slope"
                       + ("" if block_against_slope else " (not blocking: slope filter off)"))
    if would_block:
        note = ("BLOCK" if mode == "block" else "would block") + ": " + "; ".join(reasons)
    else:
        note = "ok" + (f" ({'; '.join(reasons)})" if reasons else "")
    return {"mode": mode, "sideways": r.sideways, "fails": r.fails, "dir_ok": dir_ok, "would_block": would_block,
            "reasons": reasons, "note": note,
            "metrics": {k: (v if not isinstance(v, float) else round(v, 2) if math.isfinite(v) else None)
                        for k, v in r.metrics.items()}}
