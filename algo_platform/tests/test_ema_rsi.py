"""EMA21 + RSI50 Breakout (from ema21_rsi50_candle_sl.pine) on this platform's rules."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backtest.engine import BacktestConfig, run_backtest
from brokers.base import Candle, Exchange, Tick
from core.clock import combine
from core.indicators import ATR, EMA, RSI
from storage.models import Trade
from strategies.base import Direction, TradeInfo
from strategies.ema_rsi_breakout import EmaRsiBreakout, EmaRsiBreakoutSettings
from strategies.registry import all_templates
from tests.conftest import FakeStrategyContext, at, make_candle
from tests.engine_harness import build_env


# ---------------------------------------------------------------- indicators (TradingView maths)
def test_ema_is_sma_seeded_then_recursive():
    e = EMA(3)
    assert [e.update(x) for x in (1, 2, 3, 4, 5)] == [None, None, 2.0, 3.0, 4.0]


def test_rsi_matches_wilder_reference():
    # the classic Wilder / StockCharts 14-period example
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    r = RSI(14)
    vals = [r.update(c) for c in closes]
    assert vals[13] is None
    # independent Wilder calculation: SMA of the first 14 gains / losses, then (prev * 13 + x) / 14
    ch = [b - a for a, b in zip(closes, closes[1:])]
    g = sum(max(x, 0) for x in ch[:14]) / 14
    lo = sum(max(-x, 0) for x in ch[:14]) / 14
    expected = [100 - 100 / (1 + g / lo)]
    for x in ch[14:]:
        g, lo = (g * 13 + max(x, 0)) / 14, (lo * 13 + max(-x, 0)) / 14
        expected.append(100 - 100 / (1 + g / lo))
    assert vals[14:] == pytest.approx(expected)
    assert vals[14] == pytest.approx(70.46, abs=0.01)  # StockCharts prints 70.53 from rounded averages
    up = RSI(3)
    assert [up.update(x) for x in (1, 2, 3, 4)][-1] == 100.0  # only gains


def test_rsi_peek_is_the_live_value_without_changing_state():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00]
    r = RSI(14)
    for c in closes:
        r.update(c)
    before = (r.prev, r.up.value, r.down.value, r.value)
    live = r.peek(46.40)
    assert (r.prev, r.up.value, r.down.value, r.value) == before  # nothing committed
    assert live == pytest.approx(r.update(46.40))  # = the value if the candle closed there


def test_atr_first_bar_is_high_minus_low():
    a = ATR(2)
    assert a.update(10, 8, 9) is None
    assert a.update(12, 9, 11) == pytest.approx((2 + 3) / 2)  # TR uses the previous close: max(3, 3, 0)


# ---------------------------------------------------------------- settings
def test_registered_and_defaults():
    assert all_templates()["ema_rsi_breakout"] is EmaRsiBreakout
    s = EmaRsiBreakoutSettings()
    assert (s.candle_timeframe_min, s.lots, s.ema_length, s.rsi_length, s.rsi_mid) == (5, 10, 21, 14, 50)
    assert (s.sync_bars, s.valid_bars, s.buffer, s.sl_mode) == (3, 2, 0.05, "confirmation_candle")
    assert (s.tp1_rsi, s.tp2_rsi, s.split()) == (68, 80, (4, 4))
    assert (s.entry_start, s.entry_end) == (time(9, 20), time(15, 0))
    assert s.early_exit_ema and not s.early_exit_rsi


@pytest.mark.parametrize("kw, msg", [
    ({"tp1_rsi": 80, "tp2_rsi": 75}, "TP2 RSI level must be greater"),
    ({"tp1_rsi": 45}, "above the middle level"),
    ({"allow_long": False, "allow_short": False}, "allow long"),
    ({"entry_start": "15:00", "entry_end": "09:20"}, "entry window"),
    ({"entry_end": "15:45"}, "entry window"),
    ({"underlying": "SENSEX"}, "BSE"),
    ({"tp1_lots": 5, "tp2_lots": 5}, "less than Lots"),
    ({"tp1_lots": 2, "tp2_lots": 0}, "both"),
    ({"lots": 0}, "lots"),
])
def test_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        EmaRsiBreakoutSettings(**kw)


def test_tp_off_skips_tp_checks():
    assert EmaRsiBreakoutSettings(tp_enabled=False, tp1_lots=5, tp2_lots=5).tp_enabled is False


# ---------------------------------------------------------------- strategy helpers
def tick(p: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=p)


def warm_ctx(trend: float, start: str = "09:16") -> FakeStrategyContext:
    """80 earlier 5-minute candles moving `trend` points each (falling -> price below EMA, RSI low)."""
    ctx = FakeStrategyContext(start=at(start))
    t0 = at("09:15") - timedelta(days=1)
    px = 23700.0
    for i in range(80):
        o, px = px, px + trend
        ctx.history_candles.append(make_candle(t0 + timedelta(minutes=5 * i), o, max(o, px) + 1, min(o, px) - 1, px))
    ctx.last_px = px  # type: ignore[attr-defined]
    return ctx


async def start(ctx, **kw) -> EmaRsiBreakout:
    s = EmaRsiBreakout(EmaRsiBreakoutSettings(**kw), ctx)
    await s.on_start()
    s.restore([])
    return s


async def candle(strat, ctx, t: datetime, o: float, c: float) -> None:
    ctx.clock.set(t + timedelta(minutes=5, seconds=2))
    await strat.on_candle(ctx.spot, make_candle(t, o, max(o, c) + 1, min(o, c) - 1, c))
    await strat.on_timer(ctx.now())


async def rally_until_setup(strat, ctx, step: float = 6.0) -> tuple[datetime, float]:
    """Rising candles from 09:15 until a confirmation candle arms a setup. Returns (next candle time, last close)."""
    t, px = at("09:15"), ctx.last_px
    for _ in range(20):
        o, px = px, px + step
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
        if strat.setups:
            return t, px
    raise AssertionError("no setup")


# ---------------------------------------------------------------- rules
async def test_long_setup_levels_and_entry():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    assert strat.ind.ready()  # warmed up from history
    t, px = await rally_until_setup(strat, ctx)
    x = strat.setups[0]
    assert x.direction == "CE_BUY" and x.state == "PENDING"
    assert x.trigger == round(x.conf_high + 0.05, 2) and x.stop == round(x.conf_low - 0.05, 2)  # 1-tick buffer
    assert x.valid_until == x.candle_start + timedelta(minutes=15)  # confirmation close + 2 candles
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(x.trigger - 0.05))
    assert not ctx.signals
    await strat.on_tick(tick(x.trigger))  # stop order: touch = fill
    sig = ctx.signals[0]
    assert sig.direction is Direction.CE_BUY and sig.lots == 10 and sig.option.strike % 50 == 0
    assert sig.exit_plan.spot_sl == x.stop and sig.exit_plan.spot_sl_side == "below"
    assert sig.exit_plan.exit_at == combine(t.date(), time(15, 15))  # hard exit = square-off
    assert [(p.label, p.lots, p.spot_level, p.premium_pct) for p in sig.exit_plan.partial_targets] == \
           [("tp1", 4, None, None), ("tp2", 4, None, None)]  # sold on the strategy's RSI signal
    assert x.state == "IN_TRADE"


async def test_tp1_at_rsi_68_then_tp2_at_80():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(strat.setups[0].trigger))
    seen = []
    for _ in range(20):
        o, px = px, px + 6
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
        seen.append((round(strat.ind.values["rsi"], 1), list(ctx.exit_requests)))
    tp1 = next(i for i, (_, req) in enumerate(seen) if req)
    tp2 = next(i for i, (_, req) in enumerate(seen) if len(req) == 2)
    assert seen[tp1][0] >= 68 and (tp1 == 0 or seen[tp1 - 1][0] < 68)
    assert seen[tp2][0] >= 80 and seen[tp2 - 1][0] < 80
    assert ctx.exit_requests == [("T1", "tp1", "tp1"), ("T1", "tp2", "tp2")]
    assert strat.setups[0].tp_done == ["tp1", "tp2"]  # the last 2 lots wait for SL / early exit / 15:15


async def test_early_exit_on_close_back_below_ema():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(strat.setups[0].trigger))
    for _ in range(5):
        o, px = px, px - 15
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
        if ctx.exit_requests:
            break
    assert ctx.exit_requests == [("T1", "early_exit_ema", None)]
    assert strat.ind.last.close < strat.ind.values["ema"]


async def test_setup_expires_without_breakout_and_on_close_back():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx)
    for _ in range(2):  # two quiet candles above the EMA, no tick reaches the trigger
        await candle(strat, ctx, t, px, px + 0.5)
        t += timedelta(minutes=5)
    assert strat.setups[0].state == "EXPIRED" and strat.setups[0].result == "no_breakout"
    ctx2 = warm_ctx(-2)
    strat2 = await start(ctx2)
    t, px = await rally_until_setup(strat2, ctx2)
    await candle(strat2, ctx2, t, px, px - 60)  # closes back below the EMA
    assert strat2.setups[0].state == "EXPIRED" and strat2.setups[0].result == "ema_close_back"
    ctx2.clock.set(t + timedelta(minutes=6))
    await strat2.on_tick(tick(strat2.setups[0].trigger + 5))
    assert not ctx2.signals


async def test_short_mirror_buys_pe_and_tp_at_rsi_32():
    ctx = warm_ctx(+2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx, step=-6.0)
    x = strat.setups[0]
    assert x.direction == "PE_BUY" and x.trigger == round(x.conf_low - 0.05, 2) and x.stop == round(x.conf_high + 0.05, 2)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(x.trigger))
    assert ctx.signals[0].direction is Direction.PE_BUY and ctx.signals[0].exit_plan.spot_sl_side == "above"
    for _ in range(20):
        o, px = px, px - 6
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
        if ctx.exit_requests:
            break
    assert ctx.exit_requests[0] == ("T1", "tp1", "tp1") and strat.ind.values["rsi"] <= 32


async def test_entry_window_and_tp_off():
    ctx = warm_ctx(-2, start="14:40")
    strat = await start(ctx)
    t, px = at("14:40"), ctx.last_px
    for _ in range(6):  # 14:40 .. 15:05: setups after 15:00 are not armed
        o, px = px, px + 6
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
    assert all(x.candle_start + timedelta(minutes=5) < at("15:00") for x in strat.setups)
    if strat.setups:
        ctx.clock.set(at("15:00", 1))
        await strat.on_tick(tick(strat.setups[-1].trigger + 1))
        assert not ctx.signals  # no new entries from 15:00
    # TP off: the script's single RSI 70 exit
    ctx2 = warm_ctx(-2)
    strat2 = await start(ctx2, tp_enabled=False)
    t, px = await rally_until_setup(strat2, ctx2)
    ctx2.clock.set(t + timedelta(seconds=40))
    await strat2.on_tick(tick(strat2.setups[0].trigger))
    assert ctx2.signals[0].exit_plan.partial_targets == []
    for _ in range(20):
        o, px = px, px + 6
        await candle(strat2, ctx2, t, o, px)
        t += timedelta(minutes=5)
        if ctx2.exit_requests:
            break
    assert ctx2.exit_requests == [("T1", "rsi_70", None)] and strat2.ind.values["rsi"] >= 70


async def test_restart_keeps_setups_and_never_reenters():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx)
    no = strat.setups[0].no
    # restart while the setup is pending: it is still there and can still fill
    strat2 = await start(ctx)
    assert [(x.no, x.state) for x in strat2.setups] == [(no, "PENDING")]
    # crash while entering -> after the restart the setup is never entered again
    async def crash(signal):
        ctx.signals.append(signal)
        raise SystemExit
    ctx.submit_entry = crash  # type: ignore[method-assign]
    ctx.clock.set(t + timedelta(seconds=40))
    try:
        await strat2.on_tick(tick(strat2.setups[0].trigger))
    except SystemExit:
        pass
    del ctx.submit_entry
    strat3 = await start(ctx)
    assert strat3.setups[0].state == "REJECTED" and strat3.setups[0].result == "interrupted_entry"
    await strat3.on_tick(tick(strat3.setups[0].trigger + 1))
    assert len(ctx.signals) == 1
    # a closed trade comes back as done
    strat4 = EmaRsiBreakout(EmaRsiBreakoutSettings(), ctx)
    await strat4.on_start()
    strat4.restore([TradeInfo(trade_id="T9", slot_id=strat4.setups[0].slot_id, direction="CE_BUY", status="CLOSED",
                              exit_reason="spot_stop_loss")])
    assert strat4.setups[0].state == "DONE" and strat4.setups[0].trade_id == "T9"


# ---------------------------------------------------------------- engine: strategy-requested exits
async def test_engine_exit_by_ref_partial_then_full(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "TP", "template": "nifty_ref_breakout_tp"}])
    try:
        env.ref_candle("09:45", 23639.20, 23609.20)
        env.spot(23630.0)
        env.option_price(23650, "CE", 100.0)
        await env.at("09:50", 6)
        await env.at("09:52", 31)
        env.spot(23660.0)
        await env.wait_for(lambda: env.platform.trades.live)
        lt = next(iter(env.platform.trades.live.values()))
        env.option_price(23650, "CE", 110.0)  # below the +20 % premium target: only the strategy's request sells
        assert await env.platform.trades.exit_by_ref(lt.ref, "tp1", partial="tp1")
        assert lt.open_qty == 195
        assert not await env.platform.trades.exit_by_ref(lt.ref, "tp1", partial="tp1")  # each target once
        assert await env.platform.trades.exit_by_ref(lt.ref, "early_exit_ema")
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.scalar(select(Trade))
        assert t.exit_reason == "early_exit_ema" and t.gross_pnl == pytest.approx(10 * 325)
    finally:
        await env.platform.stop()


# ---------------------------------------------------------------- backtest
def _minutes(day: date, start_px: float, per_min: float) -> list[Candle]:
    out, px, t = [], start_px, combine(day, time(9, 15))
    while t < combine(day, time(15, 30)):
        o, px = px, px + per_min
        out.append(Candle(start=t, timeframe_min=1, open=o, high=max(o, px) + 0.3, low=min(o, px) - 0.3, close=px))
        t += timedelta(minutes=1)
    return out


async def test_backtest_warms_up_on_earlier_days_and_scales_out():
    d1, d2 = date(2026, 11, 3), date(2026, 11, 4)
    candles = _minutes(d1, 24000.0, -0.4) + _minutes(d2, 24000.0 - 0.4 * 375, +1.2)
    res = await run_backtest(BacktestConfig(template_id="ema_rsi_breakout", params={}, day_from=d2, day_to=d2,
                                            slippage_pct=0), candles)
    assert res["days"] == 1  # the first day is warm-up only
    tr = res["trades"][0]
    assert tr["day"] == "2026-11-04" and tr["direction"] == "CE_BUY" and tr["qty"] == 650
    assert tr["partials"].startswith("tp1:260@") and ", tp2:260@" in tr["partials"]
    assert tr["exit_reason"] == "timed_exit" and tr["exit_time"] == "15:15:00"  # last 2 lots: hard exit
    # RSI recorded for the trades table: at entry, at each TP sell, at the final exit
    assert 50 < tr["rsi_in"] < 68 and tr["rsi_out"] > 80
    assert "tp1:260@" in tr["partials"] and tr["partials"].count("(RSI ") == 2


def test_backtest_trades_table_shows_rsi_only_when_recorded():
    from app.web.render import templates

    base = {"trade_id": "B1", "day": "2026-11-09", "slot": 1, "ref_start": "09:30", "direction": "CE_BUY",
            "strike": 24950, "signal_time": "10:00:01", "entry_spot": 24950.0, "spot_sl": 24920.0,
            "entry_premium": 100.0, "exit_time": "10:30:00", "exit_spot": 24990.0, "exit_premium": 130.0,
            "exit_reason": "trailing_stop", "partials": "", "spot_points": 40.0, "gross": 19500.0, "net": 19300.0}
    res = {"summary": {}, "exit_reasons": {}, "assumptions": {}, "events": [], "equity": [], "daily": []}
    src = open("app/web/templates/backtest_result.html").read()
    block = templates.env.from_string(src[src.index("{% set show_rsi"):src.index("<details")])  # the trades table

    def render(trades):
        return block.render(res={**res, "trades": trades})
    html = render([{**base, "rsi_in": 55.43, "rsi_out": 71.08}])
    assert "RSI in" in html and "55.4" in html and "71.1" in html
    old = render([{**base}])  # a run saved before RSI was recorded, or a slot strategy
    assert "RSI in" not in old


def test_signals_table_renders():
    from app.web.render import templates

    rt = {"strategy": {"signals": [{"no": 1, "time": "09:30", "direction": "CE_BUY", "conf_high": 23565.0,
                                    "conf_low": 23557.0, "trigger": 23565.05, "stop": 23556.95, "valid_until": "09:45",
                                    "state": "IN_TRADE", "result": None, "trade_id": "T1", "tp_done": ["tp1"]}],
                       "indicators": {"ema": 23558.6, "rsi": 50.9, "candle": "09:30", "close": 23564.0},
                       "pending_settings": False}}
    html = templates.get_template("partials/slots.html").render(rt=rt, views={}, totals={})
    assert "23,565.05" in html and "23,556.95" in html and "TP1" in html and "RSI 50.9" in html


async def test_incomplete_candle_is_replaced_by_the_brokers():
    ctx = warm_ctx(-2)
    strat = await start(ctx)
    t = at("09:15")
    full = make_candle(t, ctx.last_px, ctx.last_px + 3, ctx.last_px - 3, ctx.last_px + 1)
    ctx.candles[t] = full
    partial = make_candle(t, ctx.last_px + 1, ctx.last_px + 2, ctx.last_px, ctx.last_px + 2).model_copy(
        update={"complete": False})
    ctx.clock.set(t + timedelta(minutes=5, seconds=2))
    await strat.on_candle(ctx.spot, partial)
    assert strat.ind.last.close == full.close


async def _in_trade(**kw):
    ctx = warm_ctx(-2)
    strat = await start(ctx, **kw)
    t, px = await rally_until_setup(strat, ctx)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(strat.setups[0].trigger))
    assert strat.setups[0].state == "IN_TRADE"
    return ctx, strat, t, px


def _price_for_rsi(strat, level: float, up: bool = True) -> float:
    """Smallest move from the last close whose live RSI reaches `level`."""
    last = strat.ind.last.close
    for k in range(1, 20000):
        p = last + k * 0.05 if up else last - k * 0.05
        r = strat.ind.rsi.peek(p)
        if (r >= level) if up else (r <= level):
            return round(p, 2)
    raise AssertionError("level not reachable")


async def test_tp1_tp2_fire_mid_candle_on_live_rsi():
    ctx, strat, t, px = await _in_trade()
    rsi_at_close = strat.ind.values["rsi"]
    assert rsi_at_close < 68
    p68 = _price_for_rsi(strat, 68)
    ctx.clock.set(t + timedelta(minutes=2))  # middle of the forming candle
    await strat.on_tick(tick(p68 - 0.05))
    assert ctx.exit_requests == []  # live RSI just below 68
    await strat.on_tick(tick(p68))
    assert ctx.exit_requests == [("T1", "tp1", "tp1")]
    await strat.on_tick(tick(p68 + 1))
    assert len(ctx.exit_requests) == 1  # each target once
    p80 = _price_for_rsi(strat, 80)
    await strat.on_tick(tick(p80))
    assert ctx.exit_requests == [("T1", "tp1", "tp1"), ("T1", "tp2", "tp2")]
    assert any("(live)" in m for m in ctx.messages("TP_SIGNAL"))


async def test_candle_close_mode_waits_for_the_close():
    ctx, strat, t, px = await _in_trade(rsi_exit_on="candle_close")
    p68 = _price_for_rsi(strat, 68)
    ctx.clock.set(t + timedelta(minutes=2))
    await strat.on_tick(tick(p68 + 2))
    assert ctx.exit_requests == []
    await candle(strat, ctx, t, px, p68 + 2)  # the candle closes up there
    assert ctx.exit_requests == [("T1", "tp1", "tp1")]


async def test_pe_mid_candle_tp_at_live_rsi_32():
    ctx = warm_ctx(+2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx, step=-6.0)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(strat.setups[0].trigger))
    p32 = _price_for_rsi(strat, 32, up=False)
    ctx.clock.set(t + timedelta(minutes=2))
    await strat.on_tick(tick(p32 + 0.05))
    assert ctx.exit_requests == []
    await strat.on_tick(tick(p32))
    assert ctx.exit_requests == [("T1", "tp1", "tp1")]


# ---------------------------------------------------------------- trailing stop after TP1
async def test_trail_starts_only_after_tp1_and_only_tightens():
    ctx, strat, t, px = await _in_trade()
    a = strat.setups[0]
    atr = strat.ind.values["atr"]
    ctx.clock.set(t + timedelta(minutes=1))
    p68 = _price_for_rsi(strat, 68)
    await strat.on_tick(tick(p68 - 0.05))  # new high, but TP1 not reached yet: no trailing
    assert ctx.stop_updates == []
    await strat.on_tick(tick(p68))  # TP1 sells; the trail starts from the best price since entry
    assert ctx.exit_requests == [("T1", "tp1", "tp1")]
    level = round(p68 - 1.5 * atr, 2)
    assert ctx.stop_updates == [("T1", level)] and a.trail_stop == level
    await strat.on_tick(tick(p68 - 5))  # a pullback never loosens the stop
    await strat.on_tick(tick(p68 + 0.3))  # tightens by < 0.5 point: not sent
    assert len(ctx.stop_updates) == 1
    await strat.on_tick(tick(p68 + 4))
    assert ctx.stop_updates[-1] == ("T1", round(p68 + 4 - 1.5 * atr, 2))
    assert strat.status()["signals"][0]["trail_stop"] == ctx.stop_updates[-1][1]


async def test_trail_pe_and_switch_off():
    ctx = warm_ctx(+2)
    strat = await start(ctx)
    t, px = await rally_until_setup(strat, ctx, step=-6.0)
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(strat.setups[0].trigger))
    p32 = _price_for_rsi(strat, 32, up=False)
    await strat.on_tick(tick(p32))
    atr = strat.ind.values["atr"]
    assert ctx.stop_updates == [("T1", round(p32 + 1.5 * atr, 2))]  # PE: above the lowest price
    ctx2, strat2, t2, _ = await _in_trade(trail_after_tp1=False)
    await strat2.on_tick(tick(_price_for_rsi(strat2, 68) + 10))
    assert ctx2.exit_requests and ctx2.stop_updates == []


async def test_engine_tighten_stop_only_tightens_and_exits_as_trailing_stop(tmp_path):
    env = await build_env(tmp_path)
    try:
        env.ref_candle("09:45", 23639.20, 23609.20)
        env.spot(23630.0)
        env.option_price(23650, "CE", 100.0)
        await env.at("09:50", 6)
        await env.at("09:52", 31)
        env.spot(23660.0)
        await env.wait_for(lambda: env.platform.trades.live)
        lt = next(iter(env.platform.trades.live.values()))
        tm = env.platform.trades
        assert not tm.tighten_stop(lt.ref, 23600.0)  # lower = looser for a CE: ignored
        assert tm.tighten_stop(lt.ref, 23640.0) and lt.spot_sl == 23640.0
        assert tm.exits.states[lt.id].spot_sl == 23640.0
        with env.db.session() as s:
            assert s.get(Trade, lt.id).spot_sl == 23640.0
        env.option_price(23650, "CE", 112.0)
        env.spot(23639.0)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t = s.get(Trade, lt.id)
        assert t.exit_reason == "trailing_stop" and t.meta.get("trailed") is True
    finally:
        await env.platform.stop()
