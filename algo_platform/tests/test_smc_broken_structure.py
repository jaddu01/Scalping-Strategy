"""25-Nov-2026: after a 500-point gap-up rally SMC still read Monday's 15-min "bear" structure (no new swing was
confirmed) and only looked for shorts, with price far above the whole dealing range. A trend-timeframe close beyond
the last swing against the trend now means "side": no setups until new swings confirm."""
from __future__ import annotations

from datetime import timedelta

from brokers.base import Candle
from strategies.smc_playbook import SmcPlaybook, SmcSettings
from tests.conftest import FakeStrategyContext, at

# zig-zag down: swing highs 111 then 101 (lower high), swing lows 89 then 79 (lower low) -> bear
BEAR = [100, 105, 110, 105, 100, 95, 90, 95, 100, 95, 90, 85, 80, 85, 90]


def _feed(strat: SmcPlaybook, closes: list[float]) -> None:
    t0 = at("09:15") - timedelta(days=1)
    for i, c in enumerate(closes):
        strat.trend_s.add(Candle(start=t0 + timedelta(minutes=15 * i), timeframe_min=15, open=c, high=c + 1,
                                 low=c - 1, close=c))


def test_close_above_last_swing_high_breaks_a_bear_trend():
    strat = SmcPlaybook(SmcSettings(), FakeStrategyContext())
    _feed(strat, BEAR)
    assert strat.trend() == ("bear", 101, 79)
    _feed(strat, [103])  # closes above the last swing high (101): the down structure is broken
    assert strat.trend() == ("side", 101, 79)


def test_close_below_last_swing_low_breaks_a_bull_trend():
    strat = SmcPlaybook(SmcSettings(), FakeStrategyContext())
    _feed(strat, [200 - c for c in BEAR])  # the mirror: higher highs / higher lows
    t, hi, lo = strat.trend()
    assert t == "bull" and lo == 99
    _feed(strat, [97])  # closes below the last swing low (99)
    assert strat.trend()[0] == "side"
