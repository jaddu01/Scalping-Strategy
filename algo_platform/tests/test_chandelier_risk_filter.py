"""Chandelier risk filter: skip a signal when R (entry to SL, spot points) is below `min_risk_points` or above
`max_risk_points`; 0 = off; with the stop or the targets on. cap_targets: trade, TPs measured from the clamped R.
Also: no signal from the first candle a strategy sees in a day (late start) or after a feed gap."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from strategies.chandelier_exit import ChandelierSettings
from tests.conftest import at
from tests.test_chandelier import _first_trade


async def _first_risk() -> float:
    ctx, s = await _first_trade()
    return next(v for v in s.signals if v.trade_id).risk


def test_defaults_off_and_validation():
    d = ChandelierSettings()
    assert (d.min_risk_points, d.max_risk_points) == (0, 0)
    ChandelierSettings(min_risk_points=15, max_risk_points=50)
    ChandelierSettings(min_risk_points=15)  # max off
    with pytest.raises(ValidationError, match="must be less than"):
        ChandelierSettings(min_risk_points=50, max_risk_points=50)


async def test_wide_risk_is_skipped_and_logged():
    r = await _first_risk()
    ctx, s = await _first_trade(max_risk_points=round(r - 0.5, 2))
    x = next(v for v in s.signals if v.risk == r)
    assert (x.state, x.result, x.trade_id) == ("SKIPPED", "risk_above_max", None)
    msg = next(e["message"] for e in ctx.events if e["type"] == "RISK_SKIPPED" and e["payload"].get("setup") == x.no)
    assert f"risk {r:.2f} pts is above" in msg
    assert all(sig.meta["risk_pts"] <= r - 0.5 for sig in ctx.signals)  # any trade taken is inside the limit


async def test_small_risk_is_skipped():
    r = await _first_risk()
    ctx, s = await _first_trade(min_risk_points=round(r + 0.5, 2))
    x = next(v for v in s.signals if v.risk == r)
    assert (x.state, x.result) == ("SKIPPED", "risk_below_min")
    assert all(sig.meta["risk_pts"] >= r + 0.5 for sig in ctx.signals)


async def test_inside_the_range_trades_as_before():
    r = await _first_risk()
    ctx, s = await _first_trade(min_risk_points=max(0.0, round(r - 1, 2)), max_risk_points=round(r + 1, 2))
    assert ctx.signals and ctx.signals[0].meta["risk_pts"] == r


async def test_applies_without_a_stop_when_targets_are_on():
    r = await _first_risk()
    ctx, s = await _first_trade(use_stop_loss=False, max_risk_points=round(r - 0.5, 2))
    x = next(v for v in s.signals if v.risk == r)
    assert x.result == "risk_above_max" and any(e["type"] == "RISK_SKIPPED" for e in ctx.events)


async def test_ignored_with_stop_and_targets_off():
    r = await _first_risk()
    ctx, s = await _first_trade(use_stop_loss=False, use_targets=False, max_risk_points=round(r - 0.5, 2))
    assert ctx.signals and not any(e["type"] == "RISK_SKIPPED" for e in ctx.events)


async def test_cap_targets_trades_with_tps_from_the_limit():
    r = await _first_risk()
    lim = round(r / 2, 2)
    ctx, s = await _first_trade(max_risk_points=lim, risk_filter_action="cap_targets")
    x = next(v for v in s.signals if v.trade_id)
    assert x.risk == r and x.state != "SKIPPED"
    sg = 1 if x.long else -1
    assert (x.tp1, x.tp2, x.tp3) == pytest.approx(tuple(x.entry + sg * k * lim for k in (2, 3, 4)), abs=0.02)
    assert ctx.signals[0].exit_plan.spot_sl == x.stop  # the stop is not moved
    assert any(e["type"] == "RISK_CAPPED" for e in ctx.events)


async def test_cap_targets_below_min():
    r = await _first_risk()
    lim = round(r * 2, 2)
    ctx, s = await _first_trade(min_risk_points=lim, risk_filter_action="cap_targets")
    x = next(v for v in s.signals if v.trade_id)
    sg = 1 if x.long else -1
    assert x.tp1 == pytest.approx(x.entry + sg * 2 * lim, abs=0.02)


def _signal(s, prev_start, c_start):
    from brokers.base import Candle

    prev = Candle(start=prev_start, timeframe_min=5, open=100, high=101, low=99, close=100)
    c = Candle(start=c_start, timeframe_min=5, open=100, high=106, low=99.5, close=105)
    return s._new_signal(c, prev, True, c_start + timedelta(minutes=5))


async def test_late_start_first_candle_and_feed_gap_are_skipped():
    _, s = await _first_trade()
    late = _signal(s, at("15:25") - timedelta(days=1), at("09:40"))  # yesterday's last candle -> today 09:40
    assert late is None and s.signals[-1].result == "first_candle"
    gap = _signal(s, at("10:00"), at("10:15"))  # 10:05 and 10:10 missing
    assert gap is None and s.signals[-1].result == "previous_candle_missing"
    ok = _signal(s, at("10:10"), at("10:15"))
    assert ok is not None
