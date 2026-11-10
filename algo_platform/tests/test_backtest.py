"""Backtester: intra-bar fills happen at the first price step beyond a trigger or stop, not at the
bar's extreme; and broker candles are cached per day."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from backtest.candle_cache import CandleCache
from backtest.engine import BacktestConfig, _walk, run_backtest
from brokers.base import Candle
from core.clock import combine

DAY = date(2026, 11, 4)  # a Wednesday


def bar(hhmm: str, o: float, h: float, l: float, c: float, day: date = DAY) -> Candle:
    hh, mm = map(int, hhmm.split(":"))
    return Candle(start=combine(day, time(hh, mm)), timeframe_min=1, open=o, high=h, low=l, close=c)


def flat_day(day: date = DAY, price: float = 23600.0) -> dict[datetime, Candle]:
    out = {}
    t = combine(day, time(9, 15))
    while t < combine(day, time(15, 30)):
        out[t] = Candle(start=t, timeframe_min=1, open=price, high=price + 1, low=price - 1, close=price)
        t += timedelta(minutes=1)
    return out


def test_walk_steps_through_the_bar():
    ticks = _walk(bar("09:50", 100.0, 120.0, 99.0, 118.0), 0.5)
    prices = [p for _, p, _ in ticks]
    assert prices[0] == 100.0 and prices[-1] == 118.0
    assert max(prices) == 120.0 and min(prices) == 99.0
    # no jump bigger than one step inside the bar
    assert max(abs(b - a) for a, b in zip(prices, prices[1:])) <= 0.5 + 1e-9
    times = [t for t, _, _ in ticks]
    assert times == sorted(times)


async def test_entry_fills_near_trigger_not_at_bar_high():
    bars = flat_day()
    # reference 09:45-09:50: high 23610, low 23590 -> long trigger 23612.25
    for i in range(5):
        t = combine(DAY, time(9, 45 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    # 09:52 bar rips 30 points straight through the trigger
    bars[combine(DAY, time(9, 52))] = bar("09:52", 23605.0, 23640.0, 23604.0, 23638.0)
    cfg = BacktestConfig(template_id="nifty_ref_breakout", params={}, day_from=DAY, day_to=DAY)
    res = await run_backtest(cfg, list(bars.values()))
    first = res["trades"][0]
    assert first["direction"] == "CE_BUY" and first["trigger"] == 23612.25
    assert 23612.25 < first["entry_spot"] <= 23612.25 + 0.5  # old engine filled at the bar high, 23640


async def test_spot_stop_fills_near_the_stop_level():
    bars = flat_day()
    for i in range(5):
        t = combine(DAY, time(9, 45 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    bars[combine(DAY, time(9, 51))] = bar("09:51", 23608.0, 23615.0, 23607.0, 23614.0)  # CE entry
    bars[combine(DAY, time(9, 53))] = bar("09:53", 23600.0, 23601.0, 23570.0, 23572.0)  # crashes through 23590
    cfg = BacktestConfig(template_id="nifty_ref_breakout", params={}, day_from=DAY, day_to=DAY)
    res = await run_backtest(cfg, list(bars.values()))
    t = res["trades"][0]
    assert t["exit_reason"] == "spot_stop_loss"
    assert 23589.5 <= t["exit_spot"] <= 23590.0  # old engine exited at the bar low, 23570


async def test_candle_cache_fetches_only_missing_days(tmp_path):
    cache = CandleCache(tmp_path)
    d1, d2 = date(2026, 11, 3), date(2026, 11, 4)
    data = {**flat_day(d1), **flat_day(d2, 23700.0)}
    calls = []

    async def fetch(start, end):
        calls.append((start.date(), end.date()))
        return [c for t, c in data.items() if start <= t <= end]

    so, eo = (lambda d: combine(d, time(9, 15))), (lambda d: combine(d, time(15, 30)))
    rows, cached, fetched = await cache.get_range("NSE", "NIFTY", d1, d2, date(2026, 11, 6), fetch, so, eo)
    assert (cached, fetched) == (0, 2) and len(rows) == 750
    rows2, cached2, fetched2 = await cache.get_range("NSE", "NIFTY", d1, d2, date(2026, 11, 6), fetch, so, eo)
    assert (cached2, fetched2) == (2, 0) and len(calls) == 1
    assert [(c.start, c.close) for c in rows2] == [(c.start, c.close) for c in rows]
    # today's partial day is never cached
    await cache.get_range("NSE", "NIFTY", d2, d2, d2, fetch, so, eo)
    assert len(calls) == 2
    assert cache.export_range("NSE", "NIFTY", d1, d2).count("\n") == 751


async def test_exit_is_at_the_fixed_time():
    """No trailing: a winning CE is held to the hard exit (reference start + 15 min - 2 s = 09:59:58)."""
    bars = flat_day()
    for i in range(5):
        t = combine(DAY, time(9, 45 + i))
        bars[t] = Candle(start=t, timeframe_min=1, open=23600, high=23610, low=23590, close=23600)
    bars[combine(DAY, time(9, 51))] = bar("09:51", 23608.0, 23616.0, 23607.0, 23615.0)  # CE entry
    for i in range(52, 60):  # rally then a pullback that stays above the 23590 stop
        t = combine(DAY, time(9, i))
        px = 23630.0 if i < 56 else 23600.0
        bars[t] = Candle(start=t, timeframe_min=1, open=px, high=px + 1, low=px - 1, close=px)
    cfg = BacktestConfig(template_id="nifty_ref_breakout", params={}, day_from=DAY, day_to=DAY)
    res = await run_backtest(cfg, list(bars.values()))
    t = res["trades"][0]
    assert t["exit_reason"] == "timed_exit" and t["exit_time"] == "09:59:58"
    assert t["spot_sl"] == 23590.0
