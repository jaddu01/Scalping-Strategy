"""SMC multi_r take-profits: TP1 / TP2 at R multiples, the rest a runner (trailed on the 15-min swings, a target, or
the breakeven stop), with the stop stepping to TP1 once TP2 is reached. Off by default."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from brokers.base import Candle
from strategies.smc_playbook import SmcSettings
from tests.conftest import at
from tests.test_smc import _bar, play, started, tick


def test_defaults_keep_the_single_target():
    s = SmcSettings()
    assert s.tp_mode == "single" and s.runner_exit == "trail_15m"


@pytest.mark.parametrize("kw, msg", [
    ({"tp1_lots": 5, "tp2_lots": 5}, "at least one lot is the runner"),
    ({"tp1_rr": 2.0, "tp2_rr": 2.0}, "TP1 must be below TP2"),
    ({"partial_lots_at_1r": 2}, "TP1 replaces it"),
    ({"runner_exit": "target", "runner_rr": 2.0}, "beyond TP2"),
])
def test_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        SmcSettings(tp_mode="multi_r", **kw)


async def _multi(**kw):
    ctx, s = await started(tp_mode="multi_r", lots=5, **kw)
    await play(ctx, s)
    return ctx, s, s.setups[0], ctx.signals[0]


async def test_tp1_tp2_partials_and_trailed_runner_without_target():
    ctx, s, x, sig = await _multi()
    risk = x.entry_spot - x.stop
    plan = sig.exit_plan
    assert [(p.label, p.lots) for p in plan.partial_targets] == [("tp1", 2), ("tp2", 2)]
    assert plan.partial_targets[0].spot_level == pytest.approx(x.entry_spot + risk, abs=0.01) == x.tp1
    assert plan.partial_targets[1].spot_level == pytest.approx(x.entry_spot + 2 * risk, abs=0.01) == x.tp2
    assert plan.tp_spot_level is None and x.target is None  # the runner has no target
    assert "TP1=" in [e for e in ctx.events if e["type"] == "SETUP"][0]["message"]
    # +1R: breakeven lock as before; TP2: the stop jumps to TP1
    await s.on_tick(tick(x.tp1))
    lock = round(x.entry_spot + 0.3 * risk, 2)
    await s.on_tick(tick(x.tp2))
    assert ctx.stop_updates == [(x.trade_id, lock), (x.trade_id, x.tp1)] and x.runner_on and x.stop == x.tp1
    assert any(e["type"] == "RUNNER" for e in ctx.events)


async def test_runner_trails_the_15min_swings_after_tp2():
    ctx, s, x, sig = await _multi(runner_step_floor=False)
    await s.on_tick(tick(x.tp2))
    assert x.runner_on
    before = len(ctx.stop_updates)
    base = x.tp2 + 20
    t = at("10:00")
    for i, (o, c, h, l) in enumerate([(base, base + 0.5, base + 1, base), (base, base - 4, base + 2, base - 5),
                                      (base - 4, base - 8, base + 3, base - 13), (base - 8, base, base + 6, base - 7),
                                      (base, base + 10, base + 13, base)]):
        await s.on_candle(ctx.spot, Candle(start=t + timedelta(minutes=15 * i), timeframe_min=15, open=o, high=h,
                                           low=l, close=c))
    ctx.clock.set(at("11:01", 1))
    await s.on_candle(ctx.spot, _bar(at("11:00"), base + 10, base + 11, 1, False))
    trail = round(base - 13 - 0.2 * (s.entry.atr_value or x.atr), 2)
    assert ctx.stop_updates[before:] == [(x.trade_id, trail)] and x.stop == trail
    assert any(e["type"] == "TRAIL" and "runner" in e["message"] for e in ctx.events)


async def test_no_trail_before_tp2():
    ctx, s, x, sig = await _multi()
    s._cont_trail()
    assert ctx.stop_updates == []


async def test_runner_target_and_breakeven_only():
    ctx, s, x, sig = await _multi(runner_exit="target", runner_rr=4.0)
    risk = x.entry_spot - x.stop
    assert sig.exit_plan.tp_spot_level == pytest.approx(x.entry_spot + 4 * risk, abs=0.01)
    ctx2, s2, x2, sig2 = await _multi(runner_exit="breakeven_only")
    assert sig2.exit_plan.tp_spot_level is None


async def test_single_mode_unchanged():
    ctx, s = await started(lots=5)
    await play(ctx, s)
    x, sig = s.setups[0], ctx.signals[0]
    assert sig.exit_plan.partial_targets == [] and x.tp1 is None and sig.exit_plan.tp_spot_level == x.target
