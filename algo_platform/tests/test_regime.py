"""Sideways (chop) filter: core/regime.py and its use in the EMA21 + RSI50 strategy."""
from __future__ import annotations

import math
from datetime import timedelta

from core.regime import RegimeConfig, check_regime, direction_allowed, summarize
from tests.conftest import at
from tests.test_ema_rsi import rally_until_setup, start, tick, warm_ctx


def _candles(closes: list[float], wick: float = 1.0) -> list[dict]:
    out, prev = [], closes[0]
    for c in closes:
        out.append({"open": prev, "high": max(prev, c) + wick, "low": min(prev, c) - wick, "close": c})
        prev = c
    return out


def trend(n: int = 120, step: float = 6.0) -> list[dict]:
    return _candles([24000 + step * i + (3 if i % 2 else 0) for i in range(n)])


def chop(n: int = 120) -> list[dict]:
    return _candles([24000 + 12 * math.sin(i * 1.3) for i in range(n)], wick=4)


# ---------------------------------------------------------------- core/regime.py
def test_trend_is_not_sideways():
    r = check_regime(trend())
    assert not r.sideways and r.fails < 2, r.reasons
    assert r.metrics["ema_slope_atr"] > 0.3 and r.metrics["adx"] > 20
    assert direction_allowed("CE_BUY", r) and not direction_allowed("PE_BUY", r)


def test_chop_is_sideways_with_reasons():
    r = check_regime(chop())
    assert r.sideways and r.fails >= 2
    assert any("flat EMA" in x for x in r.reasons) and set(r.metrics) == {
        "ema", "atr", "ema_slope_atr", "adx", "chop", "crosses", "range_atr"}


def test_not_enough_candles_blocks_to_be_safe():
    r = check_regime(trend(20))
    assert r.sideways and "not enough candles" in r.reasons[0] and r.metrics == {}


def test_config_thresholds_change_the_verdict():
    r = check_regime(trend(), RegimeConfig(min_range_atr=1000, max_chop=1, min_fails=2))
    assert r.sideways and any("tight box" in x for x in r.reasons) and any("CHOP" in x for x in r.reasons), r.reasons
    loose = check_regime(chop(), RegimeConfig(min_fails=6))  # more fails needed than there are checks
    assert not loose.sideways and loose.fails >= 2


def test_summarize_notes_and_slope_rule():
    up = check_regime(trend())
    s = summarize(up, "PE_BUY")  # against the slope, filter off -> recorded, not blocking
    assert not s["would_block"] and not s["dir_ok"] and "against the EMA slope" in s["note"]
    assert s["note"].startswith("ok")
    s = summarize(up, "PE_BUY", block_against_slope=True, mode="block")
    assert s["would_block"] and s["note"].startswith("BLOCK:")
    s = summarize(check_regime(chop()), "CE_BUY")
    assert s["would_block"] and s["note"].startswith("would block:") and s["mode"] == "log_only"
    assert all(v is None or isinstance(v, (int, float)) for v in s["metrics"].values())


# ---------------------------------------------------------------- in the strategy
async def test_log_only_records_and_never_blocks():
    ctx = warm_ctx(-2)
    strat = await start(ctx, regime_min_range_atr=50, regime_max_chop=1)  # forced "sideways"
    t, _ = await rally_until_setup(strat, ctx)
    x = strat.setups[0]
    assert x.state == "PENDING" and x.regime["would_block"] and x.regime["mode"] == "log_only"
    ev = next(e for e in ctx.events if e["type"] == "SETUP")
    assert "regime=would block" in ev["message"] and ev["payload"]["regime"]["reasons"]
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(x.trigger))
    meta = ctx.signals[0].meta  # saved with the trade
    assert meta["regime"]["would_block"] and meta["regime_entry"]["metrics"]["adx"] is not None
    assert strat.status()["signals"][0]["regime"].startswith("would block")


async def test_block_skips_the_setup():
    ctx = warm_ctx(-2)
    strat = await start(ctx, regime_filter="block", regime_min_range_atr=50, regime_max_chop=1)
    t = at("09:15")
    from tests.test_ema_rsi import candle
    px = ctx.last_px
    for _ in range(20):
        o, px = px, px + 6
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
        if strat.setups:
            break
    x = strat.setups[0]
    assert (x.state, x.result) == ("SKIPPED", "sideways") and strat.active is None
    assert any(e["type"] == "REGIME_BLOCKED" for e in ctx.events)
    await strat.on_tick(tick(x.trigger + 50))
    assert not ctx.signals


async def test_block_rechecks_at_the_breakout():
    ctx = warm_ctx(-2)
    strat = await start(ctx, regime_filter="block")
    t, _ = await rally_until_setup(strat, ctx)
    x = strat.setups[0]
    assert x.state == "PENDING" and not x.regime["would_block"]
    strat.settings = strat.settings.model_copy(update={"regime_min_range_atr": 50, "regime_max_chop": 1})
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(x.trigger))
    assert not ctx.signals and (x.state, x.result) == ("SKIPPED", "sideways_at_entry")


async def test_off_computes_nothing():
    ctx = warm_ctx(-2)
    strat = await start(ctx, regime_filter="off", regime_min_range_atr=50, regime_max_chop=1)
    t, _ = await rally_until_setup(strat, ctx)
    x = strat.setups[0]
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(x.trigger))
    assert x.regime is None and ctx.signals[0].meta["regime"] is None


async def test_rebuild_keeps_candles_warm():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    n = len(strat.ind.candles)
    strat.apply_settings(strat.settings.model_copy(update={"ema_length": 9}))
    assert len(strat.ind.candles) == n and strat.ind.ready()


def test_tables_show_the_regime_note():
    from app.web.render import templates

    rt = {"strategy": {"signals": [{"no": 1, "time": "09:30", "direction": "CE_BUY", "conf_high": 1.0, "conf_low": 1.0,
                                    "trigger": 1.0, "stop": 1.0, "valid_until": "09:45", "state": "SKIPPED",
                                    "result": "sideways", "trade_id": None, "tp_done": [],
                                    "regime": "BLOCK: flat EMA (slope 0.10 ATR < 0.3)", "regime_block": True}],
                       "indicators": {"ema": None, "rsi": None, "candle": None, "close": None},
                       "pending_settings": False}}
    html = templates.get_template("partials/slots.html").render(rt=rt, views={}, totals={})
    assert "BLOCK: flat EMA" in html

    base = {"trade_id": "B1", "day": "2026-11-09", "slot": 1, "ref_start": "09:30", "direction": "CE_BUY",
            "strike": 24950, "signal_time": "10:00:01", "entry_spot": 24950.0, "spot_sl": 24920.0,
            "entry_premium": 100.0, "exit_time": "10:30:00", "exit_spot": 24990.0, "exit_premium": 130.0,
            "exit_reason": "trailing_stop", "spot_points": 40.0, "gross": -300.0, "net": -500.0, "partials": "",
            "regime": "would block: weak trend (ADX 14.0 < 20.0)", "regime_block": True}
    src = open("app/web/templates/backtest_result.html").read()
    block = templates.env.from_string(src[src.index("{% set show_rsi"):src.index("<details")])
    res = {"summary": {}, "exit_reasons": {}, "assumptions": {}, "events": []}
    html = block.render(res={**res, "trades": [base, {**base, "trade_id": "B2", "net": 900.0, "regime": "ok",
                                                      "regime_block": False}]})
    assert "<b>1</b> of 2 trades would have been blocked" in html and "weak trend" in html
