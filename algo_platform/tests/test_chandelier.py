"""Chandelier Exit + Multi-TP (strategies/chandelier_exit.py, from chandelier_exit_multi_tp.pine)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from brokers.base import Candle, Exchange, Tick
from core.clock import combine
from strategies.base import Direction, TradeInfo
from strategies.chandelier_exit import ChandelierExit, ChandelierLines, ChandelierSettings
from strategies.registry import all_templates, default_settings
from tests.conftest import DAY, FakeStrategyContext, at


def series(days: int = 3, seed: float = 0.0) -> list[Candle]:
    """5-min candles over `days` sessions ending on DAY: waves big enough for the Chandelier to flip."""
    out, prev, k = [], 24000.0, 0
    for d in range(days - 1, -1, -1):
        day = DAY - timedelta(days=d)
        for i in range(75):
            t = combine(day, at("09:15").time()) + timedelta(minutes=5 * i)
            p = 24000 + 60 * math.sin((k + seed) / 9) + 25 * math.sin((k + seed) / 3.1)
            out.append(Candle(start=t, timeframe_min=5, open=prev, high=max(prev, p) + 3, low=min(prev, p) - 3, close=p))
            prev, k = p, k + 1
    return out


def pine_reference(candles: list[Candle], length: int = 7, mult: float = 2.0) -> list[tuple[int, str | None]]:
    """A line-by-line copy of the Pine script (ta.atr = RMA seeded with the SMA; use close for extremums)."""
    out, trs, rma, prev_c, lsp_v, ssp_v, d = [], [], None, None, None, None, 1
    for i, c in enumerate(candles):
        tr = c.high - c.low if prev_c is None else max(c.high - c.low, abs(c.high - prev_c), abs(c.low - prev_c))
        trs.append(tr)
        if len(trs) < length:
            out.append((d, None)); prev_c = c.close; continue
        rma = sum(trs[:length]) / length if len(trs) == length else (rma * (length - 1) + tr) / length
        atr = mult * rma
        closes = [x.close for x in candles[i - length + 1:i + 1]]
        ls, ss = max(closes) - atr, min(closes) + atr
        lsp = ls if lsp_v is None else lsp_v
        ssp = ss if ssp_v is None else ssp_v
        if prev_c > lsp: ls = max(ls, lsp)
        if prev_c < ssp: ss = min(ss, ssp)
        nd = 1 if c.close > ssp else -1 if c.close < lsp else d
        sig = "buy" if nd == 1 and d == -1 else "sell" if nd == -1 and d == 1 else None
        d, lsp_v, ssp_v, prev_c = nd, ls, ss, c.close
        out.append((d, sig))
    return out


async def started(history: list[Candle], **kw) -> tuple[FakeStrategyContext, ChandelierExit]:
    ctx = FakeStrategyContext(start=at("09:14"))
    ctx.history_candles = history
    s = ChandelierExit(ChandelierSettings(**kw), ctx)
    await s.on_start()
    s.restore([])
    return ctx, s


async def feed(ctx, s, candles, stop_at=None):
    for c in candles:
        ctx.clock.set(c.start + timedelta(minutes=5, seconds=1))
        ctx.spot_ltp = c.close
        await s.on_candle(ctx.spot, c)
        await s.on_timer(ctx.now())
        if stop_at is not None and stop_at(s):
            return c


def tick(p: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=p)


def split(all_c):
    today = [c for c in all_c if c.start.date() == DAY]
    return [c for c in all_c if c.start.date() < DAY], today


# ---------------------------------------------------------------- the lines = the Pine script
def test_lines_and_signals_match_the_pine_script():
    cs = series(4)
    ref = pine_reference(cs)
    lines = ChandelierLines(ChandelierSettings())
    got = [(lines.update(c), lines.dir) for c in cs]
    assert [sig for sig, _ in got] == [sig for _, sig in ref]
    assert [d for _, d in got][10:] == [d for d, _ in ref][10:]
    assert sum(1 for _, sig in ref if sig) >= 6  # the waves really flip it


# ---------------------------------------------------------------- template + settings
def test_registered_defaults_and_validation():
    assert all_templates()["chandelier_exit"] is ChandelierExit
    d = default_settings("chandelier_exit")
    assert (d["candle_timeframe_min"], d["atr_length"], d["atr_multiplier"], d["use_close"]) == (5, 7, 2.0, True)
    assert (d["tp1_rr"], d["tp2_rr"], d["tp3_rr"], d["tp1_lots"], d["tp2_lots"], d["tp3_lots"]) == (2, 3, 4, 4, 4, 2)
    assert d["skip_first_candle"] and not d["move_sl_to_entry_after_tp1"] and d["exit_on_opposite"]
    s = ChandelierSettings()
    assert s.lots == 10 and s.step == 50 and ChandelierSettings(underlying="SENSEX", exchange="BSE").step == 100
    for kw, msg in (({"tp2_rr": 2.0}, "targets must increase"), ({"tp1_lots": 0, "tp2_lots": 0, "tp3_lots": 0}, "more than 0"),
                    ({"underlying": "SENSEX"}, "trades on BSE")):
        with pytest.raises(ValidationError, match=msg):
            ChandelierSettings(**kw)


def test_lot_split_is_personal_and_shown_as_lots():
    from app.web.shared import PERSONAL_PARAMS, lots_of, personal_keys

    assert {"tp1_lots", "tp2_lots", "tp3_lots"} <= set(personal_keys(ChandelierSettings))
    assert "tp3_lots" in PERSONAL_PARAMS
    assert lots_of({"tp1_lots": 4, "tp2_lots": 4, "tp3_lots": 2}) == 10 and lots_of({"lots": 5}) == 5 and lots_of({}) == "–"


# ---------------------------------------------------------------- trades
async def _first_trade(**kw):
    hist, today = split(series(3))
    ctx, s = await started(hist, **kw)
    await feed(ctx, s, today, stop_at=lambda st: bool(ctx.signals))
    return ctx, s


async def test_entry_levels_lots_and_exit_plan():
    ctx, s = await _first_trade()
    assert ctx.signals, "no signal in the synthetic day"
    x = next(v for v in s.signals if v.trade_id)
    sig = ctx.signals[0]
    prev = next(c for c in ctx.history_candles + [c for c in series(3) if c.start.date() == DAY]
                if c.start == x.candle_start - timedelta(minutes=5))
    if x.long:
        assert sig.direction is Direction.CE_BUY and x.stop == round(prev.low, 2) and sig.exit_plan.spot_sl_side == "below"
    else:
        assert sig.direction is Direction.PE_BUY and x.stop == round(prev.high, 2) and sig.exit_plan.spot_sl_side == "above"
    sg = 1 if x.long else -1
    assert x.risk > 0 and x.tp1 == pytest.approx(x.entry + sg * 2 * x.risk, abs=0.02)
    assert x.tp3 == pytest.approx(x.entry + sg * 4 * x.risk, abs=0.02)
    assert sig.lots == 10 and sig.exit_plan.tp_spot_level == x.tp3 and sig.exit_plan.spot_sl == x.stop
    assert [(p.label, p.lots, p.spot_level) for p in sig.exit_plan.partial_targets] == [("tp1", 4, x.tp1), ("tp2", 4, x.tp2)]
    assert sig.exit_plan.exit_at == combine(DAY, at("15:15").time())


async def test_opposite_signal_closes_and_reverses():
    hist, today = split(series(3))
    ctx, s = await started(hist)
    first = await feed(ctx, s, today, stop_at=lambda st: bool(ctx.signals))
    a = s.active
    rest = [c for c in today if c.start > first.start]
    await feed(ctx, s, rest, stop_at=lambda st: bool(ctx.exit_requests))
    assert ctx.exit_requests and ctx.exit_requests[0] == (a.trade_id, "opposite_signal", None)
    assert a.reverse_requested and s._pending is not None and len(ctx.signals) == 1  # waits for the exit to fill
    ctx.open_position = False
    await s.on_trade_update(TradeInfo(trade_id=a.trade_id, slot_id=a.slot_id, direction=a.side, status="CLOSED",
                                      exit_reason="opposite_signal"))
    assert len(ctx.signals) == 2 and ctx.signals[1].direction.value != a.side  # the new direction
    assert a.state == "DONE" and s.active is not None and s._pending is None


async def test_no_reversal_when_switched_off():
    hist, today = split(series(3))
    ctx, s = await started(hist, exit_on_opposite=False)
    first = await feed(ctx, s, today, stop_at=lambda st: bool(ctx.signals))
    await feed(ctx, s, [c for c in today if c.start > first.start])
    assert ctx.exit_requests == [] and len(ctx.signals) == 1
    assert any(x.result == "already_in_position" for x in s.signals)


async def test_first_candle_signal_is_skipped_unless_turned_off():
    for seed in range(0, 60):  # find a series whose day starts with a flip on the 09:15 candle
        cs = series(3, seed / 3)
        ref = pine_reference(cs)
        i0 = next(i for i, c in enumerate(cs) if c.start.date() == DAY)
        if ref[i0][1]:
            break
    else:
        pytest.skip("no first-candle flip found")
    hist, today = cs[:i0], cs[i0:]
    ctx, s = await started(hist)
    await feed(ctx, s, today[:1])
    assert s.signals[0].state == "SKIPPED" and s.signals[0].result == "first_candle" and not ctx.signals
    ctx2, s2 = await started(hist, skip_first_candle=False, entry_start="09:20")
    await feed(ctx2, s2, today[:1])
    assert s2.signals[0].result != "first_candle"


async def test_stop_to_entry_after_tp1_only_when_enabled():
    for be in (False, True):
        ctx, s = await _first_trade(move_sl_to_entry_after_tp1=be)
        x = s.active
        await s.on_tick(tick(x.tp1))
        assert x.tp_hit == "tp1"
        assert ctx.stop_updates == ([(x.trade_id, x.entry)] if be else [])


async def test_cold_start_waits_for_history_and_restart_keeps_the_trade():
    ctx = FakeStrategyContext(start=at("09:14"))
    s = ChandelierExit(ChandelierSettings(), ctx)
    await s.on_start()
    assert s.cold and any(e["type"] == "WARMUP_EMPTY" for e in ctx.events)
    hist, today = split(series(3))
    await feed(ctx, s, today[:20])
    assert s.signals == [] and ctx.signals == []
    ctx.history_candles = hist
    ctx.clock.set(s._warm_retry_at + timedelta(seconds=1))
    await s.on_timer(ctx.now())
    assert not s.cold
    # restart: the open trade is kept, never entered again
    ctx3, s3 = await _first_trade()
    x = s3.active
    s4 = ChandelierExit(ChandelierSettings(), ctx3)
    await s4.on_start()
    s4.restore([TradeInfo(trade_id=x.trade_id, slot_id=x.slot_id, direction=x.side, status="OPEN")])
    assert s4.active is not None and s4.active.no == x.no


def test_signals_table_renders():
    from app.web.render import templates

    rt = {"strategy": {"ce": {"direction": "long", "long_stop": 24010.5, "short_stop": 24120.0, "candle": "10:30",
                              "close": 24060.0, "tf": 5},
                       "ce_signals": [{"no": 1, "time": "10:35", "direction": "CE_BUY", "entry": 24060.0, "stop": 24040.0,
                                       "risk": 20.0, "tp1": 24100.0, "tp2": 24120.0, "tp3": 24140.0, "tp_hit": "tp1",
                                       "be": False, "state": "IN_TRADE", "result": None, "trade_id": None,
                                       "regime": "ok", "regime_block": False}],
                       "cold": False, "pending_settings": False}}
    html = templates.get_template("partials/slots.html").render(rt=rt, views={}, totals={})
    assert "LONG" in html and "24,100.00 ✓" in html and "24,140.00" in html and "risk 20.0" in html


def test_new_strategy_form_renders(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.web import routes_auth
    from core.config import AppConfig
    from storage.crypto import Crypto
    from tests.test_auth import login, setup_admin

    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'w.db'}", master_key=Crypto.generate_key())
    routes_auth._throttle._hits.clear()
    with TestClient(create_app(cfg, start_engine=False), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        login(c)
        html = c.get("/strategies/new?template=chandelier_exit").text
        for label in ("ATR period", "ATR multiplier", "TP1 lots", "TP3 (× risk)", "Skip signals of the day&#39;s first candle",
                      "Move SL to entry after TP1 is hit", "Close remaining lots on the opposite signal", "Sideways filter"):
            assert label in html, label


# ---------------------------------------------------------------- stop-and-reverse (Chandelier_Exit.pine): no SL, no targets
async def test_no_sl_no_targets_is_stop_and_reverse():
    ctx, s = await _first_trade(use_stop_loss=False, use_targets=False)
    sig = ctx.signals[0]
    assert sig.lots == 10  # TP1 + TP2 + TP3 lots, all held
    assert sig.exit_plan.spot_sl is None and sig.exit_plan.tp_spot_level is None and sig.exit_plan.partial_targets == []
    assert sig.exit_plan.exit_at == combine(DAY, at("15:15").time())
    x = s.active
    await s.on_tick(tick(x.tp3))  # no targets: nothing is marked, no stop is moved
    assert x.tp_hit == "" and ctx.stop_updates == []
    assert s.status()["use_stop_loss"] is False and s.status()["use_targets"] is False


async def test_stop_and_reverse_still_reverses_on_the_opposite_signal():
    hist, today = split(series(3))
    ctx, s = await started(hist, use_stop_loss=False, use_targets=False)
    first = await feed(ctx, s, today, stop_at=lambda st: bool(ctx.signals))
    a = s.active
    await feed(ctx, s, [c for c in today if c.start > first.start], stop_at=lambda st: bool(ctx.exit_requests))
    assert ctx.exit_requests[0] == (a.trade_id, "opposite_signal", None)
    ctx.open_position = False
    await s.on_trade_update(TradeInfo(trade_id=a.trade_id, slot_id=a.slot_id, direction=a.side, status="CLOSED",
                                      exit_reason="opposite_signal"))
    assert len(ctx.signals) == 2 and ctx.signals[1].exit_plan.spot_sl is None


def test_switches_default_on_and_table_hides_unused_levels():
    from app.web.render import templates

    d = default_settings("chandelier_exit")
    assert d["use_stop_loss"] and d["use_targets"]  # the Multi-TP strategy is unchanged
    rt = {"strategy": {"ce": {"direction": "short", "long_stop": 1.0, "short_stop": 2.0, "candle": "10:30", "close": 1.5,
                              "tf": 5},
                       "ce_signals": [{"no": 1, "time": "10:35", "direction": "PE_BUY", "entry": 24060.0, "stop": 24080.0,
                                       "risk": 20.0, "tp1": 24020.0, "tp2": 24000.0, "tp3": 23980.0, "tp_hit": "",
                                       "be": False, "state": "IN_TRADE", "result": None, "trade_id": None,
                                       "regime": "ok", "regime_block": False}],
                       "use_stop_loss": False, "use_targets": False, "cold": False, "pending_settings": False}}
    html = templates.get_template("partials/slots.html").render(rt=rt, views={}, totals={})
    assert "held to opposite signal" in html and "24,080.00" not in html and "23,980.00" not in html


async def test_backtest_runs_without_a_stop():
    from datetime import date

    from backtest.engine import BacktestConfig, run_backtest

    minutes = []
    for c in series(4):
        for k in range(5):  # 5-min candles as 1-min bars with the same path
            p = c.open + (c.close - c.open) * (k + 1) / 5
            minutes.append(Candle(start=c.start + timedelta(minutes=k), timeframe_min=1, open=c.open if k == 0 else p,
                                  high=max(c.open, p) + 1, low=min(c.open, p) - 1, close=p))
    res = await run_backtest(BacktestConfig(template_id="chandelier_exit", day_from=DAY, day_to=DAY,
                                            params={"use_stop_loss": False, "use_targets": False}), minutes)
    assert res["trades"] and all(t["spot_sl"] is None for t in res["trades"])
    assert {t["exit_reason"] for t in res["trades"]} <= {"opposite_signal", "timed_exit"}
