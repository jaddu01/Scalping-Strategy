"""Fixes after 13-Nov-2026: one shared Angel instrument list (memory), history retried after a rate limit,
EMA / RSI never trading on cold indicators, and TP targets already passed at entry waiting for RSI to come back."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import brokers.angelone as angel
from brokers.angelone import AngelOneAdapter, AngelOneCredentials
from core.clock import now_ist
from tests.conftest import at
from tests.test_ema_rsi import _in_trade, _price_for_rsi, candle, rally_until_setup, start, tick, warm_ctx
from tests.test_regime_shared import _service

ROWS = [{"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "0", "lotsize": "1",
         "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0"},
        {"token": "43213", "symbol": "NIFTY17NOV2625150PE", "name": "NIFTY", "expiry": "17NOV2026", "strike": "2515000",
         "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5"}]


def _adapter(cid: str, cache) -> AngelOneAdapter:
    creds = AngelOneCredentials(client_id=cid, api_key="KEY", pin="1234", totp_secret="JBSWY3DPEHPK3PXP")
    return AngelOneAdapter(creds, account_id=hash(cid) % 100, cache_dir=cache)


# ---------------------------------------------------------------- one instrument list for all Angel accounts
async def test_angel_accounts_share_one_instrument_list(tmp_path, monkeypatch):
    (tmp_path / f"angel_scrip_master_{now_ist():%Y%m%d}.json").write_text(json.dumps(ROWS))
    (tmp_path / "angel_scrip_master_20200101.json").write_text("[]")  # an old day's file is removed on download
    angel._SHARED_STORES.clear()
    builds = []
    real = AngelOneAdapter._build_store_from_file.__func__

    def counting(cls, path):
        builds.append(path)
        return real(cls, path)

    monkeypatch.setattr(AngelOneAdapter, "_build_store_from_file", classmethod(counting))
    a, b, c = _adapter("A1", tmp_path), _adapter("B2", tmp_path), _adapter("C3", tmp_path)
    n = await asyncio.gather(a.load_instruments(), b.load_instruments(), c.load_instruments())
    assert n == [2, 2, 2] and len(builds) == 1  # built once, three accounts at the same moment
    assert a.store is b.store is c.store
    assert a.find_instrument(angel.Exchange.NFO, "43213").strike == 25150.0
    angel._SHARED_STORES.clear()


# ---------------------------------------------------------------- history: retried after "exceeding access rate"
async def test_history_is_retried_after_a_rate_limit():
    md, inst, feed, clock = await _service()
    md.history_retry_delays = (0.0, 0.0)
    real, fails = feed.get_historical_candles, [2]

    async def flaky(*a, **k):
        if fails[0]:
            fails[0] -= 1
            raise RuntimeError("getCandleData: rate limited by Angel (Access denied because of exceeding access rate)")
        return await real(*a, **k)

    feed.get_historical_candles = flaky
    events = []
    md.bus.add_sink(events.append)
    assert len(await md.history(inst, 5, 300)) == 81  # third attempt works
    assert sum(e.event_type == "HISTORICAL_FETCH_FAILED" for e in events) == 2
    fails[0] = 5
    clock.set(clock.now() + timedelta(minutes=1))
    assert await md.history(inst, 5, 300) == []  # gives up after 3 attempts


# ---------------------------------------------------------------- EMA / RSI: no trading on cold indicators
async def test_ema_rsi_waits_for_history_instead_of_trading_cold():
    ctx = warm_ctx(-2)
    hist, ctx.history_candles = ctx.history_candles, []  # history fails at start (rate limit, broker error)
    strat = await start(ctx)
    assert strat.cold and any(e["type"] == "WARMUP_EMPTY" for e in ctx.events)
    t, px = at("09:15"), ctx.last_px
    for _ in range(6):  # a rally that would arm a setup on warm indicators
        o, px = px, px + 6
        await candle(strat, ctx, t, o, px)
        t += timedelta(minutes=5)
    assert strat.setups == [] and strat.status()["cold"]
    # history comes back: the next retry warms up on it (today's candles included) and setups resume
    ctx.history_candles = hist
    ctx.clock.set(strat._warm_retry_at + timedelta(seconds=1))
    await strat.on_timer(ctx.now())
    assert not strat.cold and any("history loaded on retry" in e["message"] for e in ctx.events)
    strat2 = await start(warm_ctx(-2))
    assert not strat2.cold  # normal start: warm at once


async def test_retry_waits_longer_each_time():
    ctx = warm_ctx(-2)
    ctx.history_candles = []
    strat = await start(ctx)
    first = strat._warm_retry_at
    ctx.clock.set(first + timedelta(seconds=1))
    await strat.on_timer(ctx.now())
    assert strat.cold and strat._warm_retry_at - ctx.now() > first - at("09:16")  # the pause grew
    assert sum(e["type"] == "WARMUP_EMPTY" for e in ctx.events) == 1  # warned once, not every retry


# ---------------------------------------------------------------- TP already passed at entry waits
async def test_tp_already_passed_at_entry_waits_for_rsi_to_come_back():
    ctx = warm_ctx(-2)
    strat = await start(ctx, tp1_rsi=51, tp2_rsi=90)
    t, px = await rally_until_setup(strat, ctx)
    x = strat.setups[0]
    ctx.clock.set(t + timedelta(seconds=40))
    await strat.on_tick(tick(x.trigger))
    assert x.state == "IN_TRADE" and strat.ind.rsi.peek(x.trigger) >= 51
    assert x.tp_wait == ["tp1"] and ctx.exit_requests == []  # not sold at the entry price
    assert any(e["type"] == "TP_WAIT" for e in ctx.events)
    await strat.on_tick(tick(x.trigger + 1))
    assert ctx.exit_requests == []  # still above 51 since entry: waiting
    below = _price_for_rsi(strat, 50.5, up=False)
    await strat.on_tick(tick(below))  # RSI comes back under 51 ...
    assert x.tp_wait == [] and ctx.exit_requests == []
    await strat.on_tick(tick(_price_for_rsi(strat, 51)))  # ... and reaches it again: TP1 sells now
    assert ctx.exit_requests == [("T1", "tp1", "tp1")]


async def test_tp_normal_when_not_passed_at_entry():
    ctx, strat, t, px = await _in_trade()
    assert strat.setups[0].tp_wait == []
    await strat.on_tick(tick(_price_for_rsi(strat, 68)))
    assert ctx.exit_requests == [("T1", "tp1", "tp1")]
