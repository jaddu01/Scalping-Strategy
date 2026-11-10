"""SENSEX (BSE index, options on BFO) works the same way as NIFTY."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from brokers.base import Exchange, Instrument, InstrumentType, Tick
from storage.models import Trade
from strategies.base import Direction
from strategies.nifty_ref_breakout import NiftyRefBreakout, NiftyRefBreakoutSettings, SlotState
from tests.conftest import FakeStrategyContext, at, make_candle
from tests.engine_harness import build_env
from tests.mock_broker import SENSEX_TOKEN

SENSEX = {"underlying": "SENSEX", "exchange": "BSE", "strike_step": 100, "buffer_points": 7.5}


def test_sensex_settings_validation():
    s = NiftyRefBreakoutSettings(**SENSEX)
    assert s.exchange == "BSE" and s.strike_step == 100
    assert NiftyRefBreakoutSettings(underlying="sensex", exchange="BSE").underlying == "SENSEX"
    with pytest.raises(Exception, match="trades on BSE"):
        NiftyRefBreakoutSettings(underlying="SENSEX")  # exchange left at NSE
    with pytest.raises(Exception, match="not a BSE index"):
        NiftyRefBreakoutSettings(underlying="NIFTY", exchange="BSE")
    assert NiftyRefBreakoutSettings(underlying="BANKNIFTY", strike_step=100).exchange == "NSE"


async def test_sensex_signal_uses_bse_index_and_bfo_options():
    ctx = FakeStrategyContext()
    ctx.spot = Instrument(exchange=Exchange.BSE, token="99919000", tradingsymbol="SENSEX", underlying="SENSEX",
                          instrument_type=InstrumentType.INDEX)
    strat = NiftyRefBreakout(NiftyRefBreakoutSettings(**SENSEX), ctx)
    assert strat.spot_key.exchange is Exchange.BSE and strat.spot_key.underlying == "SENSEX"
    await strat.on_start()
    ctx.candles[at("09:45")] = make_candle(at("09:45"), 79010, 79040, 78980, 79020)
    ctx.clock.set(at("09:50", 5))
    await strat.on_timer(ctx.now())
    slot = strat.slots[0]
    assert slot.state is SlotState.WATCHING
    assert (slot.long_trigger, slot.short_trigger) == (79047.5, 78972.5)
    ctx.clock.set(at("09:51"))
    await strat.on_tick(Tick(exchange=Exchange.BSE, token=ctx.spot.token, ltp=79060.0))
    sig = ctx.signals[0]
    assert sig.direction is Direction.CE_BUY
    assert sig.option.exchange is Exchange.BFO and sig.option.underlying == "SENSEX"
    assert sig.option.strike == 79100.0  # nearest 100
    assert ctx.last_expiry_exchange is Exchange.BFO
    assert sig.exit_plan.spot_sl == 78980 and sig.exit_plan.exit_at == at("09:59", 58)


async def test_sensex_trade_end_to_end(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "SENSEX-T", "params": SENSEX}])
    try:
        env.mock.history[at("09:45")] = make_candle(at("09:45"), 79010, 79040, 78980, 79020)
        env.mock.push(SENSEX_TOKEN, 79020)
        env.option_price(79100, "CE", 250.0, underlying="SENSEX")
        await env.at("09:50", 6)
        assert any("REF slot=#1" in m for m in env.messages("REF_CANDLE"))
        await env.at("09:51", 0)
        env.mock.push(SENSEX_TOKEN, 79060)
        await env.wait_for(lambda: env.platform.trades.live)
        with env.db.session() as s:
            t = s.scalar(select(Trade))
        assert t.exchange == "BFO" and t.tradingsymbol.startswith("SENSEX") and t.tradingsymbol.endswith("79100CE")
        assert t.quantity == 5 * 20 and t.entry_price == 250.0  # lot size from the instrument master
        await env.wait_for(lambda: env.platform.trades.live[t.id].protective_sl_status == "TRIGGER_PENDING")
        env.option_price(79100, "CE", 200.0, underlying="SENSEX")
        env.mock.push(SENSEX_TOKEN, 78980)  # spot SL at ref low
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            t2 = s.get(Trade, t.id)
        assert t2.exit_reason == "spot_stop_loss" and t2.gross_pnl == pytest.approx((200 - 250) * 100)
    finally:
        await env.platform.stop()


async def test_simulator_streams_sensex():
    from brokers.paper import PaperCredentials, SimulatedMarket
    from core.clock import FakeClock

    m = SimulatedMarket(PaperCredentials(simulate_market=True), FakeClock(at("10:00")), seed=3)
    idx = m.store.by_token(Exchange.BSE, SENSEX_TOKEN)
    assert idx.underlying == "SENSEX" and idx.instrument_type is InstrumentType.INDEX
    exp = m.store.option_expiries("SENSEX", Exchange.BFO)
    assert exp and all(d.weekday() == 3 for d in exp)  # Thursday weeklies
    for _ in range(20):
        m.step()
    assert m.candles(1, at("09:00"), at("11:00"), SENSEX_TOKEN)


async def test_spot_mismatch_never_trades():
    """Configured for NIFTY but watching SENSEX prices (the 06-Nov bug): reject, never build an order."""
    ctx = FakeStrategyContext()
    ctx.spot = Instrument(exchange=Exchange.BSE, token="99919000", tradingsymbol="SENSEX", underlying="SENSEX",
                          instrument_type=InstrumentType.INDEX)
    strat = NiftyRefBreakout(NiftyRefBreakoutSettings(), ctx)  # NIFTY / NSE
    await strat.on_start()
    ctx.candles[at("09:45")] = make_candle(at("09:45"), 79810, 79857, 79601, 79845)
    ctx.clock.set(at("09:50", 5))
    await strat.on_timer(ctx.now())
    ctx.clock.set(at("09:51"))
    await strat.on_tick(Tick(exchange=Exchange.BSE, token=ctx.spot.token, ltp=79870.0))
    assert ctx.signals == []
    assert strat.slots[0].result == "spot_mismatch"
    assert any("spot_mismatch" in m for m in ctx.messages("SIGNAL_REJECTED"))


async def test_switching_running_instance_from_nifty_to_sensex(tmp_path):
    """Editing underlying/exchange on a running instance must switch its market data too."""
    env = await build_env(tmp_path, instances=[{"name": "IDX"}])  # starts as NIFTY at 09:49
    try:
        iid = env.instance_ids[0]
        rt = env.platform.strategies.runtimes[iid]
        await env.wait_for(lambda: rt.strategy.spot is not None)
        assert rt.strategy.spot.underlying == "NIFTY"
        applied = await env.platform.strategies.update_params(
            iid, {**SENSEX, "protective_sl_pct": 0})
        assert applied
        await env.wait_for(lambda: env.messages("INSTRUMENTS_CHANGED"))
        assert rt.strategy.spot.underlying == "SENSEX" and rt.strategy.spot.exchange is Exchange.BSE
        assert "SENSEX" in env.messages("INSTRUMENTS_CHANGED")[0]
        env.ref_candle("09:45", 79040.0, 78980.0)
        env.mock.push(SENSEX_TOKEN, 79020.0)
        env.option_price(79100, "CE", 200.0, underlying="SENSEX")
        await env.at("09:50", 6)
        assert env.messages("REF_CANDLE")
        await env.at("09:52", 0)
        env.mock.push(SENSEX_TOKEN, 79060.0)
        await env.wait_for(lambda: env.platform.trades.live)
        lt = next(iter(env.platform.trades.live.values()))
        assert lt.symbol.startswith("SENSEX") and lt.exchange is Exchange.BFO and lt.option_key.strike == 79100
    finally:
        await env.platform.stop()
