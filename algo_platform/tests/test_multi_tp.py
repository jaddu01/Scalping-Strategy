"""More take-profits: Chandelier Exit + Multi-TP up to TP5, EMA21 + RSI50 Breakout up to TP4.
Off by default (TP4 / TP5 lots 0, TP3 / TP4 lots 0): existing runs behave exactly as before."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.web.shared import lots_of, personal_keys
from strategies.chandelier_exit import ChandelierSettings, Signal
from strategies.ema_rsi_breakout import EmaRsiBreakoutSettings
from tests.conftest import DAY, at
from tests.test_chandelier import _first_trade, tick
from tests.test_ema_rsi import _in_trade, _price_for_rsi


# ---------------------------------------------------------------- Chandelier: TP1-TP5
def test_chandelier_defaults_unchanged():
    s = ChandelierSettings()
    assert (s.tp4_lots, s.tp5_lots, s.tp4_rr, s.tp5_rr, s.lots) == (0, 0, 5.0, 6.0, 10)
    assert s.targets() == [("tp1", 2.0, 4), ("tp2", 3.0, 4), ("tp3", 4.0, 2)]


def test_chandelier_targets_and_lots():
    s = ChandelierSettings(tp1_lots=3, tp2_lots=2, tp3_lots=2, tp4_lots=2, tp5_lots=1)
    assert s.lots == 10
    assert s.targets() == [("tp1", 2.0, 3), ("tp2", 3.0, 2), ("tp3", 4.0, 2), ("tp4", 5.0, 2), ("tp5", 6.0, 1)]
    only5 = ChandelierSettings(tp1_lots=4, tp2_lots=4, tp3_lots=1, tp5_lots=1)  # TP4 skipped
    assert [t[0] for t in only5.targets()] == ["tp1", "tp2", "tp3", "tp5"]
    assert lots_of({"tp1_lots": 3, "tp2_lots": 2, "tp3_lots": 2, "tp4_lots": 2, "tp5_lots": 1}) == 10
    assert {"tp4_lots", "tp5_lots"} <= set(personal_keys(ChandelierSettings))


@pytest.mark.parametrize("kw,msg", [
    ({"tp4_lots": 1, "tp4_rr": 4.0}, "TP4"),  # not above TP3 (4 R)
    ({"tp4_lots": 1, "tp5_lots": 1, "tp4_rr": 5.0, "tp5_rr": 5.0}, "TP5"),
    ({"tp5_lots": 1, "tp5_rr": 3.5}, "TP5"),  # TP4 off: TP5 must be above TP3
])
def test_chandelier_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        ChandelierSettings(**kw)
    ChandelierSettings(tp4_rr=1.0, tp5_rr=1.0)  # unused levels (0 lots) are not checked


async def test_chandelier_five_tps_exit_plan():
    ctx, s = await _first_trade(tp1_lots=3, tp2_lots=2, tp3_lots=2, tp4_lots=2, tp5_lots=1)
    x = next(v for v in s.signals if v.trade_id)
    sig = ctx.signals[0]
    sg = 1 if x.long else -1
    assert x.tp4 == pytest.approx(x.entry + sg * 5 * x.risk, abs=0.05)
    assert x.tp5 == pytest.approx(x.entry + sg * 6 * x.risk, abs=0.05)
    assert sig.lots == 10
    # TP1-TP4 sell their lots; TP5 (the last) closes the rest
    assert [(p.label, p.lots, p.spot_level) for p in sig.exit_plan.partial_targets] == [
        ("tp1", 3, x.tp1), ("tp2", 2, x.tp2), ("tp3", 2, x.tp3), ("tp4", 2, x.tp4)]
    assert sig.exit_plan.tp_spot_level == x.tp5
    assert "TP5=" in ctx.messages("SETUP")[-1]
    assert s.status()["tp_labels"] == ["tp1", "tp2", "tp3", "tp4", "tp5"]
    await s.on_tick(tick(x.tp4))
    assert x.tp_hit == "tp1+tp2+tp3+tp4"


async def test_chandelier_three_tps_exit_plan_unchanged():
    ctx, s = await _first_trade()
    x = next(v for v in s.signals if v.trade_id)
    sig = ctx.signals[0]
    assert x.tp4 is None and x.tp5 is None
    assert [p.label for p in sig.exit_plan.partial_targets] == ["tp1", "tp2"] and sig.exit_plan.tp_spot_level == x.tp3
    assert s.status()["tp_labels"] == ["tp1", "tp2", "tp3"]


def test_chandelier_old_saved_setup_still_loads():
    old = {"no": 1, "side": "CE_BUY", "candle_start": at("10:30").isoformat(), "entry": 24060.0, "stop": 24040.0,
           "risk": 20.0, "tp1": 24100.0, "tp2": 24120.0, "tp3": 24140.0, "state": "IN_TRADE", "trade_id": "T1"}
    x = Signal.from_state(old)
    assert (x.tp4, x.tp5) == (None, None) and [k for k, _ in x.levels()] == ["tp1", "tp2", "tp3"]


def test_chandelier_table_shows_tp4_tp5_columns():
    from app.web.render import templates

    sig = {"no": 1, "time": "10:35", "direction": "CE_BUY", "entry": 24060.0, "stop": 24040.0, "risk": 20.0,
           "tp1": 24100.0, "tp2": 24120.0, "tp3": 24140.0, "tp4": 24160.0, "tp5": 24180.0, "tp_hit": "tp1+tp2",
           "be": False, "state": "IN_TRADE", "result": None, "trade_id": None, "regime": "ok", "regime_block": False}
    rt = {"strategy": {"ce": {"direction": "long", "long_stop": 1.0, "short_stop": 2.0, "candle": "10:30", "close": 1.5,
                              "tf": 5}, "ce_signals": [sig], "tp_labels": ["tp1", "tp2", "tp3", "tp4", "tp5"],
                       "use_stop_loss": True, "use_targets": True, "cold": False, "pending_settings": False}}
    html = templates.get_template("partials/slots.html").render(rt=rt, views={}, totals={})
    assert "<th class=\"num\">TP5</th>" in html and "24,180.00" in html and "24,120.00 ✓" in html


async def test_chandelier_backtest_sells_five_tps():
    from backtest.engine import BacktestConfig, run_backtest
    from brokers.base import Candle
    from tests.test_chandelier import series

    minutes = []
    for c in series(4):
        for k in range(5):
            p = c.open + (c.close - c.open) * (k + 1) / 5
            minutes.append(Candle(start=c.start + timedelta(minutes=k), timeframe_min=1, open=c.open if k == 0 else p,
                                  high=max(c.open, p) + 1, low=min(c.open, p) - 1, close=p))
    params = {"tp1_rr": 0.5, "tp2_rr": 0.75, "tp3_rr": 1.0, "tp4_rr": 1.25, "tp5_rr": 1.5,
              "tp1_lots": 3, "tp2_lots": 2, "tp3_lots": 2, "tp4_lots": 2, "tp5_lots": 1}
    res = await run_backtest(BacktestConfig(template_id="chandelier_exit", day_from=DAY, day_to=DAY, params=params),
                             minutes)
    assert res["trades"] and all(t["qty"] == 10 * 65 for t in res["trades"])
    full = [t for t in res["trades"] if t["exit_reason"] == "take_profit"]
    assert full, "no trade reached TP5 in the synthetic data"
    assert all([p.split(":")[0] for p in t["partials"].split(", ")] == ["tp1", "tp2", "tp3", "tp4"] for t in full)


# ---------------------------------------------------------------- EMA21 + RSI50: TP1-TP4
def test_ema_defaults_unchanged():
    s = EmaRsiBreakoutSettings()
    assert (s.tp3_lots, s.tp4_lots, s.tp3_rsi, s.tp4_rsi, s.tp3_rsi_pe, s.tp4_rsi_pe) == (0, 0, 85, 90, 15, 10)
    assert s.targets(True) == [("tp1", 68, 4), ("tp2", 80, 4)]
    assert s.targets(False) == [("tp1", 32, 4), ("tp2", 20, 4)]


def test_ema_four_targets():
    s = EmaRsiBreakoutSettings(lots=10, tp1_lots=3, tp2_lots=3, tp3_lots=2, tp4_lots=2)
    assert s.targets(True) == [("tp1", 68, 3), ("tp2", 80, 3), ("tp3", 85, 2), ("tp4", 90, 2)]
    assert s.targets(False) == [("tp1", 32, 3), ("tp2", 20, 3), ("tp3", 15, 2), ("tp4", 10, 2)]
    EmaRsiBreakoutSettings(lots=10, tp1_lots=1, tp2_lots=1, tp3_lots=1, tp4_lots=1)  # a runner may stay


@pytest.mark.parametrize("kw,msg", [
    ({"tp3_lots": 2}, "set each of TP1-TP3 lots"),
    ({"tp4_lots": 2, "tp1_lots": 3, "tp2_lots": 3}, "TP4 needs TP3"),
    ({"tp1_lots": 3, "tp2_lots": 3, "tp3_lots": 2, "tp3_rsi": 79}, "must rise: TP1 < TP2 < TP3"),
    ({"tp1_lots": 4, "tp2_lots": 4, "tp3_lots": 3}, "more than Lots"),
    ({"tp1_lots": 3, "tp2_lots": 3, "tp3_lots": 3, "tp4_lots": 3}, "more than Lots"),
    ({"tp1_lots": 3, "tp2_lots": 3, "tp3_lots": 2, "tp4_lots": 2, "tp3_rsi": 79}, "must rise"),
    ({"tp1_lots": 3, "tp2_lots": 3, "tp3_lots": 2, "tp4_lots": 2, "tp4_rsi": 85}, "must rise"),
    ({"tp1_lots": 3, "tp2_lots": 3, "tp3_lots": 2, "tp4_lots": 2, "tp3_rsi_pe": 21}, "must fall"),
])
def test_ema_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        EmaRsiBreakoutSettings(lots=10, **kw)


async def test_ema_four_tps_fire_in_order_ce():
    ctx, strat, t, px = await _in_trade(tp1_lots=3, tp2_lots=3, tp3_lots=2, tp4_lots=2)
    sig = ctx.signals[0]
    assert [(p.label, p.lots) for p in sig.exit_plan.partial_targets] == [("tp1", 3), ("tp2", 3), ("tp3", 2), ("tp4", 2)]
    ctx.clock.set(t + timedelta(minutes=2))
    want = []
    for label, level in (("tp1", 68), ("tp2", 80), ("tp3", 85), ("tp4", 90)):
        p = _price_for_rsi(strat, level)
        await strat.on_tick(tick(p - 0.05))
        assert ctx.exit_requests == want  # just below the level: nothing new
        await strat.on_tick(tick(p))
        want.append(("T1", label, label))
        assert ctx.exit_requests == want


async def test_ema_two_tps_unchanged_never_sells_tp3():
    ctx, strat, t, px = await _in_trade()
    assert [p.label for p in ctx.signals[0].exit_plan.partial_targets] == ["tp1", "tp2"]
    ctx.clock.set(t + timedelta(minutes=2))
    await strat.on_tick(tick(_price_for_rsi(strat, 90)))
    assert [r[1] for r in ctx.exit_requests] == ["tp1", "tp2"]


async def test_ema_four_tps_pe():
    from tests.test_ema_rsi import rally_until_setup, start, warm_ctx

    ctx = warm_ctx(+2)
    strat = await start(ctx, tp1_lots=3, tp2_lots=3, tp3_lots=2, tp4_lots=2)
    t, px = await rally_until_setup(strat, ctx, step=-6.0)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(strat.setups[0].trigger))
    ctx.clock.set(t + timedelta(minutes=2))
    for level in (32, 20, 15, 10):
        await strat.on_tick(tick(_price_for_rsi(strat, level, up=False)))
    assert [r[1] for r in ctx.exit_requests] == ["tp1", "tp2", "tp3", "tp4"]


def test_ema_three_targets_tp4_off():
    s = EmaRsiBreakoutSettings(lots=10, tp1_rsi=58, tp2_rsi=65, tp3_rsi=70, tp1_rsi_pe=42, tp2_rsi_pe=35,
                               tp3_rsi_pe=30, tp1_lots=3, tp2_lots=3, tp3_lots=2)
    assert s.targets(True) == [("tp1", 58, 3), ("tp2", 65, 3), ("tp3", 70, 2)]
    assert s.targets(False) == [("tp1", 42, 3), ("tp2", 35, 3), ("tp3", 30, 2)]
    # TP4's level is ignored while TP4 is off, even if it would not be in order
    EmaRsiBreakoutSettings(lots=10, tp1_lots=3, tp2_lots=3, tp3_lots=2, tp3_rsi=88, tp4_rsi=60)


async def test_ema_three_tps_fire_and_two_lots_run_on():
    ctx, strat, t, px = await _in_trade(tp1_rsi=58, tp2_rsi=65, tp3_rsi=70, tp1_lots=3, tp2_lots=3, tp3_lots=2)
    sig = ctx.signals[0]
    assert [(p.label, p.lots) for p in sig.exit_plan.partial_targets] == [("tp1", 3), ("tp2", 3), ("tp3", 2)]
    ctx.clock.set(t + timedelta(minutes=2))
    for level in (58, 65, 70, 90):
        await strat.on_tick(tick(_price_for_rsi(strat, level)))
    assert [r[1] for r in ctx.exit_requests] == ["tp1", "tp2", "tp3"]  # no TP4; 2 lots stay for the trail / stop
