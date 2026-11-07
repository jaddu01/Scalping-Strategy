"""Black-Scholes pricing used by the simulated market and the backtest premium model."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from core.clock import IST, combine

EXPIRY_CLOSE = time(15, 30)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, years: float, iv: float, is_call: bool, rate: float = 0.065) -> float:
    if years <= 0 or iv <= 0:
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        return intrinsic
    sq = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def years_to_expiry(now: datetime, expiry: date) -> float:
    end = combine(expiry, EXPIRY_CLOSE)
    secs = (end - now.astimezone(IST)).total_seconds()
    # floor at 15 minutes so expiry-day premiums keep some time value
    return max(secs, 900.0) / (365.0 * 24 * 3600)


def next_weekly_expiries(today: date, weekday: int = 1, count: int = 4) -> list[date]:
    """Next `count` weekly expiries on `weekday` (Mon=0 ... Tue=1), including today if it matches."""
    out = []
    d = today
    while len(out) < count:
        if d.weekday() == weekday:
            out.append(d)
        d += timedelta(days=1)
    return out


def option_premium(spot: float, strike: float, expiry: date, now: datetime, iv: float, is_call: bool) -> float:
    price = bs_price(spot, strike, years_to_expiry(now, expiry), iv, is_call)
    return max(0.05, round(price / 0.05) * 0.05)
