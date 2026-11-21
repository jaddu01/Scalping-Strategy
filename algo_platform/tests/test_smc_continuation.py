"""SMC continuation entries (entry_model = reversal_continuation): break of structure in premium + retrace into the
FVG, impulse / zone stop, 15-min trail or 2R target, breakeven choice, daily limit, SETUP_ENDED logging, restart."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from brokers.base import Candle
from strategies.base import Direction
from strategies.smc_playbook import Setup, SmcSettings
from tests.conftest import at
from tests.test_smc import _bar, play, started, tick

CONT = {"entry_model": "reversal_continuation"}

# trend (history): bull, range 24080-24160, equilibrium 24120. Today stays in premium (above 24120):
# swing high 09:48 (24140), swing low 09:52 (24126), break of structure 09:56 (close 24146, body 12),
# 09:55 high 24134 < 09:57 low 24141 = FVG 24134-24141; 09:57 / 09:58 touch it with red closes; 09:59 closes green above
DAY_BARS = (
    [("09:%02d" % (15 + i), 24112.0 + 0.45 * i, 24112.45 + 0.45 * i, 24112.75 + 0.45 * i, 24111.7 + 0.45 * i)
     for i in range(30)]  # 09:15-09:44: a steady drift up, no swings (each high / low above the last)
    + [("09:45", 24125.5, 24126.0, 24126.3, 24125.2)]
    + [("09:46", 24126.0, 24130.0, 24130.5, 24125.5),
       ("09:47", 24130.0, 24133.0, 24133.5, 24129.5),
       ("09:48", 24133.0, 24138.0, 24140.0, 24132.5),   # swing high 24140
       ("09:49", 24138.0, 24135.0, 24137.0, 24134.5),
       ("09:50", 24135.0, 24131.0, 24134.0, 24130.5),
       ("09:51", 24131.0, 24129.0, 24131.5, 24128.5),
       ("09:52", 24129.0, 24127.0, 24129.5, 24126.0),   # swing low 24126
       ("09:53", 24127.0, 24129.0, 24129.5, 24127.5),
       ("09:54", 24129.0, 24131.0, 24131.5, 24128.5),
       ("09:55", 24131.0, 24133.0, 24134.0, 24130.5),
       ("09:56", 24134.0, 24146.0, 24147.0, 24133.5),   # BOS: close above 24140 (09:55 closed below), big body
       ("09:57", 24149.0, 24145.0, 24150.0, 24141.0),   # FVG 24134-24141; touches it, red
       ("09:58", 24145.0, 24139.0, 24146.0, 24138.0),   # inside the zone, red: no entry yet
       ("09:59", 24139.0, 24144.0, 24145.0, 24138.0)]   # green close above the zone top -> entry
)


async def _cont(**kw):
    ctx, s = await started(**{**CONT, **kw})
    await play(ctx, s, bars=DAY_BARS)
    return ctx, s


def test_defaults_keep_todays_smc():
    d = SmcSettings()
    assert (d.entry_model, d.cont_stop, d.cont_exit, d.cont_breakeven, d.cont_max_per_day) == \
           ("reversal", "impulse", "trail_15m", False, 2)
    assert (d.cont_start.strftime("%H:%M"), d.cont_end.strftime("%H:%M"), d.cont_touch_candles) == ("09:45", "14:30", 3)
    with pytest.raises(ValidationError, match="start must be before the end"):
        SmcSettings(entry_model="reversal_continuation", cont_start="14:30", cont_end="10:00")


async def test_reversal_only_ignores_trend_continuation():
    ctx, s = await started()
    await play(ctx, s, bars=DAY_BARS)
    assert ctx.signals == [] and not any(e["type"] == "BOS" for e in ctx.events)
    assert not any(x.continuation for x in s.setups)


async def test_continuation_bos_zone_touch_then_green_close_enters():
    ctx, s = await _cont()
    x = next(x for x in s.setups if x.continuation)
    assert (x.side, x.ref_swing, x.sweep_px) == ("CE_BUY", 24140.0, 24126.0)  # broke 24140; impulse low 24126
    assert (x.zone_kind, x.zone_lo, x.zone_hi) == ("FVG", 24134.0, 24141.0)
    assert len(ctx.signals) == 1 and x.state == "IN_TRADE"
    sig = ctx.signals[0]
    assert sig.direction is Direction.CE_BUY and sig.trigger == 24144.0  # the 09:59 close, not 09:57 / 09:58
    assert sig.exit_plan.spot_sl == x.stop == round(24126.0 - 0.2 * x.atr, 2)  # below the impulse low
    assert sig.exit_plan.tp_spot_level is None  # trail_15m: no target
    types = [e["type"] for e in ctx.events if e["type"] in ("BOS", "ZONE", "SETUP")]
    assert types == ["BOS", "ZONE", "SETUP"]
    msg = next(e["message"] for e in ctx.events if e["type"] == "SETUP")
    assert "continuation" in msg and "no target (trailed on 15m swings)" in msg


async def test_zone_stop_and_2r_target_options():
    ctx, s = await _cont(cont_stop="zone", cont_exit="target_2r")
    x = next(x for x in s.setups if x.continuation)
    sig = ctx.signals[0]
    assert sig.exit_plan.spot_sl == round(24134.0 - 0.2 * x.atr, 2)  # below the zone bottom
    risk = 24144.0 - x.stop
    assert sig.exit_plan.tp_spot_level == pytest.approx(24144.0 + 2 * risk, abs=0.01)


async def test_no_breakeven_for_continuation_unless_asked():
    ctx, s = await _cont()
    x = s.active
    await s.on_tick(tick(x.one_r + 1))
    assert ctx.stop_updates == []  # trailed, not moved to breakeven
    ctx2, s2 = await _cont(cont_breakeven=True)
    await s2.on_tick(tick(s2.active.one_r + 1))
    assert len(ctx2.stop_updates) == 1


async def test_stop_trails_the_15min_swing_lows_and_only_tightens():
    ctx, s = await _cont()
    x = s.active
    stop0 = x.stop
    t = at("10:00")
    for i, (o, c, h, l) in enumerate([(24150, 24150.5, 24151, 24150), (24150, 24146, 24152, 24145),  # 15-min candles:
                                      (24146, 24142, 24153, 24137), (24142, 24150, 24156, 24143),     # a swing low
                                      (24150, 24160, 24163, 24150)]):                                 # 24137, rising highs
        await s.on_candle(ctx.spot, Candle(start=t + timedelta(minutes=15 * i), timeframe_min=15, open=o, high=h,
                                           low=l, close=c))
    assert s.trend()[0] == "bull"
    ctx.clock.set(at("11:01", 1))
    await s.on_candle(ctx.spot, _bar(at("11:00"), 24160, 24161, 1, False))  # the trail is checked on entry candles
    new = round(24137 - 0.2 * (s.entry.atr_value or x.atr), 2)  # the 15-min swing low 24137 - buffer
    assert ctx.stop_updates == [(x.trade_id, new)] and new > stop0 and x.stop == new
    assert any(e["type"] == "TRAIL" for e in ctx.events)
    ctx.clock.set(at("11:02", 1))
    await s.on_candle(ctx.spot, _bar(at("11:01"), 24161, 24162, 1, False))
    assert len(ctx.stop_updates) == 1  # same swing: no new order, never loosened


async def test_closing_through_the_zone_cancels_and_is_logged():
    ctx, s = await started(**CONT)
    bars = [b for b in DAY_BARS if b[0] <= "09:57"] + [("09:58", 24140.0, 24130.0, 24141.0, 24129.0)]
    await play(ctx, s, bars=bars)
    x = next(x for x in s.setups if x.continuation)
    assert (x.state, x.result) == ("CANCELLED", "closed_through_zone") and ctx.signals == []
    ended = [e["message"] for e in ctx.events if e["type"] == "SETUP_ENDED"]
    assert ended == [f"#{x.no} CE_BUY continuation cancelled: closed through zone"]


async def test_daily_limit_and_window():
    ctx, s = await _cont(cont_max_per_day=1)
    assert s._cont_today(at("10:00").date()) == 1
    assert sum(1 for x in s.setups if x.continuation) == 1
    ctx2, s2 = await _cont(cont_start="10:00")  # the 09:56 break is before the window
    assert ctx2.signals == [] and not any(x.continuation for x in s2.setups)


def test_old_saved_setups_load_and_unfinished_continuations_expire_on_restart():
    old = {"no": 1, "side": "CE_BUY", "sweep_time": at("10:00").isoformat(), "sweep_n": 5, "sweep_px": 24000.0,
           "level": 24010.0, "level_kind": "swing", "ref_swing": 24050.0, "atr": 5.0, "state": "DONE"}
    assert Setup.from_state(old).touch_n is None  # saved before this change
    x = Setup.from_state({**old, "state": "CZONE", "level_kind": "continuation", "touch_n": 7})
    assert x.continuation and x.touch_n == 7


async def test_restart_expires_waiting_continuation_ideas():
    ctx, s = await started(**CONT)
    await play(ctx, s, bars=[b for b in DAY_BARS if b[0] <= "09:57"])  # zone found, not entered
    assert next(x for x in s.setups if x.continuation).state == "CZONE"
    s._load(s.day)
    assert next(x for x in s.setups if x.continuation).result == "app_restarted"
