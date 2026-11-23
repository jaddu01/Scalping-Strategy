"""Chandelier risk filter: skip a signal when R (entry to SL, spot points) is below `min_risk_points` or above
`max_risk_points`; 0 = off; only with the stop on."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from strategies.chandelier_exit import ChandelierSettings
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


async def test_ignored_without_a_stop_loss():
    r = await _first_risk()
    ctx, s = await _first_trade(use_stop_loss=False, max_risk_points=round(r - 0.5, 2))
    assert ctx.signals and not any(e["type"] == "RISK_SKIPPED" for e in ctx.events)
