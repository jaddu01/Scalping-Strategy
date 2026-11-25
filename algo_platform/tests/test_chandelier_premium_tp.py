"""Chandelier Exit: TPs measured in option premium % (tp_basis = premium_pct) instead of R multiples."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.web.slot_view import slot_trade_view
from strategies.chandelier_exit import ChandelierSettings
from tests.test_chandelier import _first_trade, tick


def test_default_stays_r_multiple():
    s = ChandelierSettings()
    assert s.tp_basis == "r_multiple" and not s.premium_targets
    assert s.targets() == [("tp1", 2.0, 4), ("tp2", 3.0, 4), ("tp3", 4.0, 2)]


def test_premium_targets_and_validation():
    s = ChandelierSettings(tp_basis="premium_pct", tp1_pct=15, tp2_pct=30, tp3_pct=50)
    assert s.targets() == [("tp1", 15, 4), ("tp2", 30, 4), ("tp3", 50, 2)]
    with pytest.raises(ValidationError, match="TP1 < TP2 < TP3"):
        ChandelierSettings(tp_basis="premium_pct", tp1_pct=30, tp2_pct=30)
    with pytest.raises(ValidationError, match="TP4"):
        ChandelierSettings(tp_basis="premium_pct", tp4_lots=1, tp4_pct=60)  # not above TP3 (60 %)
    # R multiples are not checked in premium mode (and the % not in R mode)
    ChandelierSettings(tp_basis="premium_pct", tp1_rr=5, tp2_rr=3, tp3_rr=1)
    ChandelierSettings(tp1_pct=50, tp2_pct=10)


async def test_premium_exit_plan():
    ctx, s = await _first_trade(tp_basis="premium_pct", tp1_pct=20, tp2_pct=40, tp3_pct=60)
    x = next(v for v in s.signals if v.trade_id)
    plan = ctx.signals[0].exit_plan
    assert (x.tp1, x.tp2, x.tp3) == (None, None, None) and x.tp_pcts == {"tp1": 20, "tp2": 40, "tp3": 60}
    assert [(p.label, p.lots, p.spot_level, p.premium_pct) for p in plan.partial_targets] == [
        ("tp1", 4, None, 20), ("tp2", 4, None, 40)]
    assert plan.tp_spot_level is None and plan.tp_premium_pct == 60  # TP3 closes the rest
    assert plan.spot_sl == x.stop and plan.runner is None  # the stop stays on spot
    assert "TP1=+20%" in ctx.messages("SETUP")[-1]
    assert s.status()["tp_basis"] == "premium_pct"
    await s.on_tick(tick(x.entry + 500))  # spot levels do not apply: nothing marked hit
    assert x.tp_hit == ""


async def test_premium_move_sl_to_entry_uses_the_runner_floor():
    ctx, s = await _first_trade(tp_basis="premium_pct", move_sl_to_entry_after_tp1=True, tp1_lots=0, tp2_lots=5,
                                tp3_lots=5)
    r = ctx.signals[0].exit_plan.runner
    assert r is not None and r["start"] == "tp2" and r["step"] == "one_behind" and r["trail_pcts"] == [0, 0, 0]


def test_slot_view_premium_levels():
    trade = {"direction": "CE_BUY", "signal_spot": 100.0, "quantity": 150, "lot_size": 75, "entry_price": 100.0,
             "status": "CLOSED", "exit_reason": "take_profit", "tp_premium_level": 160.0, "exit_price": 140.0,
             "partials": [{"label": "tp1", "basis": "premium", "level": 120.0, "filled": 75, "price": 120.0,
                           "spot": 110.0}]}
    v = slot_trade_view(trade)
    assert v["tps"] == {"tp1": {"level": 120.0, "filled": True}, "final": {"level": 160.0, "filled": True}}


def test_table_shows_premium_targets():
    from app.web.render import templates

    sig = {"no": 1, "time": "10:35", "direction": "CE_BUY", "entry": 24060.0, "stop": 24040.0, "risk": 20.0,
           "tp1": None, "tp2": None, "tp3": None, "tp4": None, "tp5": None, "tp_pcts": {"tp1": 20, "tp2": 40, "tp3": 60},
           "tp_hit": "", "be": False, "state": "IN_TRADE", "result": None, "trade_id": "T1", "regime": "ok",
           "regime_block": False}
    rt = {"strategy": {"ce": {"direction": "long", "long_stop": 1.0, "short_stop": 2.0, "candle": "10:30", "close": 1.5,
                              "tf": 5}, "ce_signals": [sig], "tp_labels": ["tp1", "tp2", "tp3"],
                       "tp_basis": "premium_pct", "use_stop_loss": True, "use_targets": True, "cold": False,
                       "pending_settings": False}}
    trade = {"direction": "CE_BUY", "signal_spot": 24060.0, "quantity": 750, "lot_size": 75, "entry_price": 100.0,
             "status": "OPEN", "tp_premium_level": 160.0, "spot_sl": 24040.0, "spot_sl_side": "below",
             "partials": [{"label": "tp1", "basis": "premium", "level": 120.0, "filled": 300, "price": 120.0,
                           "spot": 24080.0},
                          {"label": "tp2", "basis": "premium", "level": 140.0, "filled": 0, "price": None, "spot": None}]}
    views = {"T1": slot_trade_view(trade, {"spot": 24085.0, "premium": 125.0, "open_qty": 450})}
    html = templates.get_template("partials/slots.html").render(rt=rt, views=views, totals={})
    assert "+20% ✓" in html and "₹120.00" in html and "+40%" in html and "₹160.00" in html


async def test_premium_targets_in_the_backtester():
    from datetime import timedelta

    from backtest.engine import BacktestConfig, run_backtest
    from brokers.base import Candle
    from tests.conftest import DAY
    from tests.test_chandelier import series

    minutes = []
    for c in series(4):
        for k in range(5):
            p = c.open + (c.close - c.open) * (k + 1) / 5
            minutes.append(Candle(start=c.start + timedelta(minutes=k), timeframe_min=1, open=c.open if k == 0 else p,
                                  high=max(c.open, p) + 1, low=min(c.open, p) - 1, close=p))
    params = {"tp_basis": "premium_pct", "tp1_pct": 5, "tp2_pct": 10, "tp3_pct": 15}
    res = await run_backtest(BacktestConfig(template_id="chandelier_exit", day_from=DAY, day_to=DAY, params=params),
                             minutes)
    assert res["trades"]
    sold = [t for t in res["trades"] if t["partials"]]
    assert sold and all(t["partials"].startswith("tp1:") for t in sold)
