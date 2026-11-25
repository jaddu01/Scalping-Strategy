"""Lorentzian Classification port (strategies/lorentzian_ml.py): Pine building blocks and the strategy flow."""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from brokers.base import Candle
from strategies.lorentzian_ml import (CCI, LorentzianML, LorentzianModel, LorentzianSettings, Normalize, PineEMA,
                                      Supertrend, kernel_gauss, kernel_rq)
from strategies.registry import get_template
from tests.conftest import FakeStrategyContext, at


def candle(start, o, h, l, c):  # noqa: E741
    return Candle(start=start, timeframe_min=5, open=o, high=h, low=l, close=c)


def wave(n: int, start=None, base=25000.0):
    """A deterministic up / down wave of 5-min candles (several regimes, never flat)."""
    t0 = start or at("09:15") - timedelta(days=40)
    out = []
    for i in range(n):
        mid = base + 120 * math.sin(i / 23) + 40 * math.sin(i / 5.3) + 0.05 * i
        o, c = mid - 3 * math.cos(i / 2.1), mid + 3 * math.cos(i / 1.7)
        out.append(candle(t0 + timedelta(minutes=5 * i), o, max(o, c) + 4 + (i % 3), min(o, c) - 4 - (i % 5), c))
    return out


def test_registered_and_defaults_match_the_script():
    cls = get_template("lorentzian_ml")
    s = LorentzianSettings()
    assert cls is LorentzianML and cls.warmup_days >= 30
    assert (s.neighbors_count, s.max_bars_back, s.feature_count) == (8, 2000, 5)
    assert s.features() == [("RSI", 14, 1), ("WT", 10, 11), ("CCI", 20, 1), ("ADX", 20, 2), ("RSI", 9, 1)]
    assert (s.kernel_lookback, s.kernel_weight, s.kernel_level, s.kernel_lag) == (8, 8.0, 25, 2)
    assert (s.atr_length, s.atr_multiplier, s.breakeven_rr, s.tp_rr, s.tp_lots, s.lots) == (14, 1.5, 1.0, 3.0, 5, 10)
    with pytest.raises(ValueError):
        LorentzianSettings(tp_lots=11)
    with pytest.raises(ValueError):
        LorentzianSettings(warmup_candles=1000)  # below Max bars back


def test_pine_blocks():
    e = PineEMA(3)
    assert e.update(10) == 10 and e.update(20) == 15  # seeded with the first value, alpha 0.5
    n = Normalize()
    assert n.update(5) == 0 and n.update(15) == 1 and n.update(10) == 0.5
    cci = CCI(3)
    assert cci.update(1) is None and cci.update(2) is None
    assert cci.update(3) == pytest.approx((3 - 2) / (0.015 * (2 / 3)))
    closes = [float(x) for x in range(1, 41)]  # rising: both kernels lag below the last close
    y1, y2 = kernel_rq(closes, 8, 8.0, 25), kernel_gauss(closes, 6, 25)
    assert y1 is not None and y2 is not None and y1 < 40 and y2 < 40 and y1 > closes[-27]
    assert kernel_rq(closes[:26], 8, 8.0, 25) is None  # needs x + 2 closes
    flat = [100.0] * 30
    assert kernel_rq(flat, 8, 8.0, 25) == pytest.approx(100.0)


def test_supertrend_flips_with_the_trend():
    st = Supertrend(3, 1.0)
    dirs = []
    for i, c in enumerate([100 + i for i in range(15)] + [115 - 3 * i for i in range(15)]):
        st.update(candle(at("09:15") + timedelta(minutes=5 * i), c, c + 1, c - 1, c))
        dirs.append(st.dir)
    assert dirs[12] == -1 and dirs[-1] == 1  # up trend (line below), then down trend


def test_model_votes_and_signals():
    s = LorentzianSettings(max_bars_back=400, warmup_candles=400, ema_length=50)
    m = LorentzianModel(s)
    sigs = [m.update(c) for c in wave(900)]
    assert m.ready() and len(m.pred) == s.neighbors_count
    assert -s.neighbors_count <= m.prediction <= s.neighbors_count
    assert {"buy", "sell"} <= set(sigs)  # the wave produces both kinds of entries
    assert all(abs(lbl) <= 1 for lbl in m.labels)


async def test_strategy_enters_with_atr_stop_breakeven_and_half_at_3r():
    ctx = FakeStrategyContext(start=at("09:16"))
    hist = wave(800, start=at("09:15") - timedelta(minutes=5 * 800))
    ctx.history_candles = hist
    s = LorentzianSettings(max_bars_back=400, warmup_candles=800, ema_filter=False, entry_start=at("09:15").time(),
                           regime_filter="off")
    strat = LorentzianML(s, ctx)
    await strat.on_start()
    assert not strat.cold and strat.model.ready()
    # feed today's candles until the model takes a trade
    for i, c in enumerate(wave(70, start=at("09:15"), base=hist[-1].close)):
        ctx.clock.set(c.start + timedelta(minutes=5, seconds=1))
        ctx.spot_ltp = c.close
        await strat.on_candle(ctx.spot, c)
        if ctx.signals:
            break
    assert ctx.signals, "no entry in 70 candles"
    sig = ctx.signals[0]
    x = strat.signals[-1]
    plan = sig.exit_plan
    assert plan.spot_sl == x.stop and x.risk == pytest.approx(abs(x.entry - x.stop), abs=0.02)
    assert x.risk == pytest.approx(strat.model.atr_value * 1.5, abs=0.02)
    assert [(p.label, p.lots) for p in plan.partial_targets] == [("tp1", 5)] and plan.tp_spot_level is None
    assert plan.partial_targets[0].spot_level == pytest.approx(x.entry + (3 if x.long else -3) * x.risk, abs=0.02)
    assert sig.lots == 10
