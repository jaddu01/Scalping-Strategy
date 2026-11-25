"""Dhurandhar (dhurandhar.xlsx): hourly 15-min reference candles at HH:15, buffer 5, entry until the next HH:15,
spot SL at the candle's other end, the breakout multi-TP + runner exits. Also: the backtester sells the 4 TPs by
entry premium (they were never hit before: no level)."""
from __future__ import annotations

from datetime import time, timedelta

import pytest
from pydantic import ValidationError

from backtest.engine import BacktestConfig, run_backtest
from brokers.base import Candle
from core.clock import combine
from strategies.breakout_tp import NiftyRefBreakoutTP
from strategies.dhurandhar import DhurandharBreakout, DhurandharSettings
from strategies.nifty_ref_breakout import build_slots
from strategies.registry import all_templates
from tests.test_backtest import DAY, flat_day


def test_registered_with_the_sheet_schedule():
    assert all_templates()["dhurandhar_breakout"] is DhurandharBreakout
    assert issubclass(DhurandharBreakout, NiftyRefBreakoutTP)
    s = DhurandharSettings()
    assert [t.strftime("%H:%M") for t in s.reference_candle_starts] == ["09:15", "10:15", "11:15", "12:15", "13:15",
                                                                        "14:15"]
    assert (s.candle_timeframe_min, s.buffer_points, s.lots) == (15, 5.0, 5)
    slots = build_slots(s, DAY)
    first, last = slots[0], slots[-1]
    assert (first.ref_end.time(), first.exit_at.time()) == (time(9, 30), time(10, 14, 58))
    assert first.entry_end.time() == time(10, 15)  # entries until the exit (the exit itself is 2 s earlier)
    assert last.exit_at.time() == time(15, 14, 58)


def test_same_multi_tp_and_runner_defaults():
    s = DhurandharSettings()
    assert s.tp_mode == "four_by_premium" and s.split4() == (2, 1, 1, 1)
    r = s.runner_plan()
    assert r is not None and r["start"] == "tp3" and r["step"] == "one_behind"
    # the sheet's spot targets as an option
    pts = DhurandharSettings(tp_mode="two_targets", tp_basis="points", runner_protect=False)
    assert (pts.tp1_value, pts.tp2_value, pts.split()) == (40.0, 50.0, (2, 1))
    with pytest.raises(ValidationError, match="premium_pct"):
        DhurandharSettings(tp_mode="two_targets", tp_basis="points")  # runner needs premium targets


def _breakout_day() -> list[Candle]:
    bars = flat_day()
    for i in range(15):  # reference 09:15-09:30: high 23610, low 23590 -> CE trigger 23615
        t = combine(DAY, time(9, 15 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    p = 23600.0
    for i in range(40):  # 09:35 on: a steady rally of 4 points a minute
        t = combine(DAY, time(9, 35)) + timedelta(minutes=i)
        bars[t] = Candle(start=t, timeframe_min=1, open=p, high=p + 4.5, low=p - 0.5, close=p + 4)
        p += 4
    for t in sorted(bars):
        if t >= combine(DAY, time(10, 15)):
            bars[t] = Candle(start=t, timeframe_min=1, open=p, high=p + 1, low=p - 1, close=p)
    return list(bars.values())


async def test_backtest_trades_the_breakout_and_sells_the_premium_tps():
    cfg = BacktestConfig(template_id="dhurandhar_breakout", params={"reference_candle_starts": "09:15"},
                         day_from=DAY, day_to=DAY, slippage_pct=0, iv=0.13)
    res = await run_backtest(cfg, _breakout_day())
    tr = res["trades"][0]
    assert tr["direction"] == "CE_BUY" and tr["trigger"] == 23615.0 and tr["spot_sl"] == 23590.0
    assert tr["qty"] == 5 * cfg.lot_size
    labels = [leg.split(":")[0] for leg in tr["partials"].split(", ") if leg]
    assert labels[:3] == ["tp1", "tp2", "tp3"]  # premium-band TPs are sold in the backtest too
    prem = tr["entry_premium"]
    for leg in filter(None, tr["partials"].split(", ")):
        label, rest = leg.split(":")
        need = {"tp1": 1.08, "tp2": 1.12, "tp3": 1.15, "tp4": 1.20}[label] if prem > 150 else \
            {"tp1": 1.15, "tp2": 1.22, "tp3": 1.30, "tp4": 1.40}[label]
        assert float(rest.split("@")[1]) >= prem * need - 0.06
