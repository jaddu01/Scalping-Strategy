"""SMC playbook strategy (strategies/smc_playbook.py): trend, sweep, displacement + MSS, FVG / OB zone, entry, stop /
target, breakeven, cancels, cold start, restart and settings for any index."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from brokers.base import Candle, Exchange, Tick
from core.clock import combine
from strategies.base import Direction, TradeInfo
from strategies.registry import all_templates, default_settings
from strategies.smc_playbook import SmcPlaybook, SmcSettings
from tests.conftest import DAY, FakeStrategyContext, at

PREV = DAY - timedelta(days=1)
MID = 24100.0  # mirror axis for the short-side tests


def _bar(start: datetime, o: float, c: float, tf: int, mirror: bool, h: float | None = None,
         l: float | None = None) -> Candle:
    h = max(o, c) + 1 if h is None else h
    l = min(o, c) - 1 if l is None else l
    if mirror:  # price p -> 2*MID - p: highs become lows
        o, c, h, l = 2 * MID - o, 2 * MID - c, 2 * MID - l, 2 * MID - h
    return Candle(start=start, timeframe_min=tf, open=o, high=h, low=l, close=c)


def _path(points: list[float], seg: int) -> list[float]:
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        out += [a + (b - a) * k / seg for k in range(1, seg + 1)]
    return out


def history(mirror: bool = False, trend: str = "up") -> list[Candle]:
    """15-min bars over earlier days with HH + HL (or a range), and yesterday's 1-min bars."""
    piv = ([24000, 24100, 24040, 24160, 24080, 24110] if trend == "up"
           else [24000, 24100, 24040, 24090, 24060, 24080])  # side: lower high, higher low
    closes = _path(piv, 4)
    t0 = combine(PREV, at("09:15").time()) - timedelta(days=1)
    out = []
    for i, (a, b) in enumerate(zip(closes, closes[1:])):
        out.append(_bar(t0 + timedelta(minutes=15 * i), a, b, 15, mirror))
    t1 = combine(PREV, at("09:15").time())
    zig = _path([24100, 24112, 24088, 24112, 24088, 24112, 24088, 24112, 24088, 24112, 24088, 24112, 24088, 24112,
                 24088, 24112, 24088, 24112, 24088, 24100], 20)
    for i, (a, b) in enumerate(zip(zig, zig[1:])):
        out.append(_bar(t1 + timedelta(minutes=i), a, b, 1, mirror))
    return out


TODAY = (  # (time, open, close, high, low) 1-min bars; None = high/low from open/close +-1
    [("09:%02d" % (15 + i), a, b, None, None) for i, (a, b) in
     enumerate(zip(_path([24100, 24106, 24095, 24102], 5), _path([24100, 24106, 24095, 24102], 5)[1:]))]
    + [("09:%02d" % (30 + i), 24102 - i * 0.2, 24102 - (i + 1) * 0.2, None, None) for i in range(10)]
    + [("09:40", 24100.0, 24097.0, 24101.0, 24088.0),   # sweep: below the 24094 opening-range low, closes back above
       ("09:41", 24097.0, 24112.0, 24113.0, 24096.0),   # displacement + close above the swing high = MSS
       ("09:42", 24112.0, 24115.0, 24116.0, 24111.0)]   # gap: 09:40 high 24101 < 09:42 low 24111 -> FVG; bullish close
)


async def started(mirror: bool = False, trend: str = "up", **kw) -> tuple[FakeStrategyContext, SmcPlaybook]:
    ctx = FakeStrategyContext(start=at("09:14"))
    ctx.history_candles = history(mirror, trend)
    s = SmcPlaybook(SmcSettings(**kw), ctx)
    await s.on_start()
    s.restore([])
    return ctx, s


async def play(ctx, s, mirror=False, until="23:59", bars=TODAY) -> None:
    for hm, o, c, h, l in bars:
        if hm > until:
            break
        start = at(hm)
        ctx.clock.set(start + timedelta(minutes=1, seconds=1))
        ctx.spot_ltp = None
        await s.on_candle(ctx.spot, _bar(start, o, c, 1, mirror, h, l))
        await s.on_timer(ctx.now())


def tick(p: float) -> Tick:
    return Tick(exchange=Exchange.NSE, token="99926000", ltp=p)


# ---------------------------------------------------------------- template + settings
def test_registered_with_defaults_for_any_index():
    assert all_templates()["smc_playbook"] is SmcPlaybook
    d = default_settings("smc_playbook")
    assert (d["trend_timeframe_min"], d["entry_timeframe_min"], d["lots"], d["breakeven_at_1r"]) == (15, 1, 10, True)
    assert SmcSettings().step == 50
    assert SmcSettings(underlying="BANKNIFTY").step == 100
    assert SmcSettings(underlying="sensex", exchange="BSE").step == 100
    assert SmcSettings(underlying="NIFTY", strike_step=100).step == 100


@pytest.mark.parametrize("kw, msg", [
    ({"underlying": "SENSEX"}, "trades on BSE"),
    ({"exchange": "BSE"}, "not a BSE index"),
    ({"trend_timeframe_min": 1, "entry_timeframe_min": 1}, "trend timeframe must be longer"),
    ({"partial_lots_at_1r": 10}, "fewer than Lots"),
    ({"underlying": "XYZ"}, "no automatic strike step"),
])
def test_validation(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        SmcSettings(**kw)


# ---------------------------------------------------------------- the full long setup
async def test_long_sweep_mss_fvg_entry_stop_target():
    ctx, s = await started()
    assert s.trend()[0] == "bull" and not s.cold
    await play(ctx, s)
    x = s.setups[0]
    assert x.side == "CE_BUY" and x.level == 24094 and x.level_kind in ("swing", "opening_range") and x.sweep_px == 24088
    assert (x.zone_kind, x.zone_lo, x.zone_hi) == ("FVG", 24101, 24111)
    sig = ctx.signals[0]
    assert sig.direction is Direction.CE_BUY and sig.lots == 10 and sig.option.strike == 24100
    risk = 24115 - x.stop
    assert x.stop == round(24088 - 0.2 * x.atr, 2) and sig.exit_plan.spot_sl == x.stop
    assert sig.exit_plan.spot_sl_side == "below" and sig.exit_plan.tp_spot_side == "above"
    assert sig.exit_plan.tp_spot_level == pytest.approx(24115 + 2 * risk, abs=0.01)
    assert sig.exit_plan.exit_at == combine(DAY, at("15:15").time())
    assert x.state == "IN_TRADE" and s.phase() == "IN_TRADE"
    assert [e["type"] for e in ctx.events if e["type"] in ("SWEEP", "MSS", "ZONE", "SETUP")] == \
           ["SWEEP", "MSS", "ZONE", "SETUP"]
    # breakeven: once spot is +1R the stop moves to the entry price, once
    await s.on_tick(tick(x.one_r - 0.05))
    assert ctx.stop_updates == []
    await s.on_tick(tick(x.one_r))
    await s.on_tick(tick(x.one_r + 5))
    lock = round(24115 + 0.3 * (x.one_r - 24115), 2)  # default: entry + 0.3R is locked
    assert ctx.stop_updates == [("T1", lock)] and lock > 24115
    assert "entry + 0.3R" in [e for e in ctx.events if e["type"] == "BREAKEVEN"][0]["message"]


async def test_breakeven_lock_zero_is_exact_entry():
    ctx, s = await started(breakeven_lock_r=0)
    await play(ctx, s)
    await s.on_tick(tick(s.setups[0].one_r))
    assert ctx.stop_updates == [("T1", 24115)]


async def test_short_is_the_mirror():
    ctx, s = await started(mirror=True)
    assert s.trend()[0] == "bear"
    await play(ctx, s, mirror=True)
    x = s.setups[0]
    sig = ctx.signals[0]
    assert x.side == "PE_BUY" and sig.direction is Direction.PE_BUY
    assert x.sweep_px == 2 * MID - 24088 and x.stop > x.sweep_px  # stop above the sweep high
    assert sig.exit_plan.spot_sl_side == "above" and sig.exit_plan.tp_spot_side == "below"
    assert sig.exit_plan.tp_spot_level < sig.spot


async def test_partial_at_1r_and_no_breakeven():
    ctx, s = await started(breakeven_at_1r=False, partial_lots_at_1r=5)
    await play(ctx, s)
    x, sig = s.setups[0], ctx.signals[0]
    assert [(p.label, p.lots, p.spot_level, p.side) for p in sig.exit_plan.partial_targets] == [("1R", 5, x.one_r, "above")]
    await s.on_tick(tick(x.one_r + 1))
    assert ctx.stop_updates == []


# ---------------------------------------------------------------- no trade / cancel
async def test_no_trade_when_trend_is_sideways():
    ctx, s = await started(trend="side")
    assert s.trend()[0] == "side"
    await play(ctx, s)
    assert s.setups == [] and ctx.signals == []


async def test_trading_below_the_sweep_before_mss_cancels():
    ctx, s = await started()
    bars = TODAY[:-2] + [("09:41", 24097.0, 24090.0, 24098.0, 24085.0),  # new low under the 24088 sweep
                         ("09:42", 24090.0, 24112.0, 24113.0, 24089.0)]
    await play(ctx, s, bars=bars)
    assert s.setups[0].state == "CANCELLED" and s.setups[0].result == "beyond_sweep"
    assert ctx.signals == []


async def test_wide_stop_is_skipped_when_capped():
    ctx, s = await started(max_stop_atr=1.0)
    await play(ctx, s)
    assert ctx.signals == [] and s.setups[0].state == "SKIPPED" and s.setups[0].result.startswith("stop_too_wide")


async def test_outside_entry_window_no_entry():
    ctx, s = await started(entry_start="09:50", entry_end="14:45")
    await play(ctx, s)
    assert ctx.signals == [] and s.setups[0].result == "outside_entry_window"


async def test_one_position_at_a_time():
    ctx, s = await started()
    ctx.open_position = True  # another trade of this instance / risk says no
    await play(ctx, s)
    assert ctx.signals == [] and s.setups[0].result == "already_in_position"


# ---------------------------------------------------------------- cold start + restart
async def test_cold_start_waits_for_history():
    ctx = FakeStrategyContext(start=at("09:14"))
    s = SmcPlaybook(SmcSettings(), ctx)
    await s.on_start()
    assert s.cold and any(e["type"] == "WARMUP_EMPTY" for e in ctx.events)
    await play(ctx, s, until="09:39")
    assert s.setups == []
    ctx.history_candles = history()  # back: the next retry loads it
    ctx.clock.set(s._warm_retry_at + timedelta(seconds=1))
    await s.on_timer(ctx.now())
    assert not s.cold and s.trend()[0] == "bull"


async def test_restart_keeps_open_trade_and_drops_unfinished_ideas():
    ctx, s = await started()
    await play(ctx, s)
    x = s.setups[0]
    s.setups.append(type(x).from_state({**x.to_state(), "no": 2, "state": "ZONE", "trade_id": None}))
    s._persist()
    s2 = SmcPlaybook(SmcSettings(), ctx)
    await s2.on_start()
    s2.restore([TradeInfo(trade_id="T1", slot_id=x.slot_id, direction="CE_BUY", status="OPEN")])
    assert [y.state for y in s2.setups] == ["IN_TRADE", "EXPIRED"] and s2.setups[1].result == "app_restarted"
    await s2.on_tick(tick(x.one_r))
    assert ctx.stop_updates == [("T1", round(24115 + 0.3 * (x.one_r - 24115), 2))]  # still managed after the restart


def test_setups_table_renders():
    from app.web.render import templates

    rt = {"strategy": {"smc": {"trend": "bull", "range_high": 24160.0, "range_low": 24080.0, "equilibrium": 24120.0,
                               "atr": 2.4, "last": "09:42", "close": 24115.0, "trend_tf": 15, "entry_tf": 1},
                       "setups": [{"no": 1, "time": "09:40", "direction": "CE_BUY", "state": "IN_TRADE", "sweep": 24088.0,
                                   "level": 24094.0, "level_kind": "opening_range", "mss_level": 24107.0,
                                   "zone": "FVG 24101.00–24111.00", "entry": 24115.0, "stop": 24087.5, "target": 24170.0,
                                   "be": True, "entry_time": "09:43", "trade_id": None, "result": None, "regime": "ok",
                                   "regime_block": False}],
                       "cold": False, "pending_settings": False}}
    html = templates.get_template("partials/slots.html").render(rt=rt, views={}, totals={})
    assert "BULL" in html and "FVG 24101.00–24111.00" in html and "opening range" in html and "→ BE" in html


def test_new_strategy_form_renders(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from core.config import AppConfig
    from storage.crypto import Crypto
    from tests.test_auth import login, setup_admin

    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'w.db'}", master_key=Crypto.generate_key())
    from app.web import routes_auth

    routes_auth._throttle._hits.clear()
    with TestClient(create_app(cfg, start_engine=False), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        login(c)
        r = c.get("/strategies/new?template=smc_playbook")
        assert r.status_code == 200
        html = r.text
        for label in ("Trend timeframe (minutes)", "Entry timeframe (minutes)", "Move stop to breakeven at +1R",
                      "Strike step (0 = automatic for the index)", "Sideways filter"):
            assert label in html
        assert html.index("Trend timeframe") < html.index("Swing strength") < html.index("Sideways filter")
