"""On a weekend / holiday every strategy shows the last trading day's saved slots / signals (24-Nov-2026: a restart
on a market holiday left the slots page with all-MISSED rows and empty signal tables)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from strategies.chandelier_exit import ChandelierExit, ChandelierSettings
from strategies.ema_rsi_breakout import EmaRsiBreakout, EmaRsiBreakoutSettings
from strategies.smc_playbook import SmcPlaybook, SmcSettings
from tests.conftest import DAY, FakeStrategyContext, at



@pytest.mark.parametrize("cls,settings", [(ChandelierExit, ChandelierSettings), (EmaRsiBreakout, EmaRsiBreakoutSettings),
                                          (SmcPlaybook, SmcSettings)])
async def test_start_on_holiday_loads_last_trading_day(cls, settings):
    ctx = FakeStrategyContext(start=at("20:50") + timedelta(days=1))
    ctx.holidays = {DAY + timedelta(days=1)}
    seen = []
    strat = cls(settings(), ctx)
    strat._load = lambda d: seen.append(d)  # type: ignore[method-assign]
    await strat.on_start()
    last = DAY  # Thursday; the Friday is the holiday
    assert strat.day == last and seen == [last]
    assert strat.status()["day"] == last.isoformat()


async def test_trading_day_start_is_unchanged():
    ctx = FakeStrategyContext(start=at("09:20"))
    strat = ChandelierExit(ChandelierSettings(), ctx)
    await strat.on_start()
    assert strat.day == DAY
