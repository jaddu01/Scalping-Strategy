"""End-to-end engine tests: signal → risk → order → fill → protective SL → exit → P&L,
broker-driven events, enable/disable while in trade, and instance isolation."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from brokers.base import OrderStatus, OrderType, Side
from storage.models import OrderRecord, StrategyInstance, Trade
from strategies.base import Strategy, StrategySettings
from strategies.registry import register
from tests.conftest import at
from tests.engine_harness import Env, build_env


@pytest.fixture
async def env(tmp_path):
    e = await build_env(tmp_path)
    yield e
    await e.platform.stop()


async def enter_ce(env: Env) -> Trade:
    """Reference 09:45 H=23639.20 L=23609.20 → CE breakout at 23660 → ATM 23650 CE."""
    env.ref_candle("09:45", 23639.20, 23609.20)
    env.spot(23630.0)
    env.option_price(23650, "CE", 100.0)
    await env.at("09:50", 6)
    assert env.messages("REF_CANDLE")
    await env.at("09:52", 31)
    env.spot(23660.0)
    await env.wait_for(lambda: env.platform.trades.live)
    with env.db.session() as s:
        t = s.scalar(select(Trade))
        s.expunge(t)
    return t


async def test_full_trade_spot_stop_loss(env):
    t = await enter_ce(env)
    assert t.direction == "CE_BUY"
    assert t.tradingsymbol.endswith("23650CE")
    assert t.lots == 5 and t.quantity == 5 * 65
    assert t.entry_price == 100.0
    assert t.spot_sl == 23609.20
    assert t.exit_at.strftime("%H:%M:%S") == "09:59:58"
    # protective SL-M at 40% below the fill
    await env.wait_for(lambda: env.platform.trades.live[t.id].protective_sl_status == "TRIGGER_PENDING")
    lt = env.platform.trades.live[t.id]
    assert lt.protective_sl_trigger == 60.0
    entry_line = env.messages("ENTRY")[0]
    assert entry_line.startswith("ENTRY CE_BUY symbol=") and "strike=23650 qty=325" in entry_line
    assert "spot_SL=23609.20 exit=09:59:58" in entry_line
    # spot SL: CE exits when spot <= ref_low
    env.option_price(23650, "CE", 80.0)
    env.spot(23609.20)
    await env.wait_for(lambda: not env.platform.trades.live)
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
        orders = list(s.scalars(select(OrderRecord).where(OrderRecord.trade_id == t.id)))
    assert t2.status == "CLOSED" and t2.exit_reason == "spot_stop_loss"
    assert t2.exit_price == 80.0
    assert t2.gross_pnl == pytest.approx((80 - 100) * 325)
    assert t2.charges and t2.net_pnl == pytest.approx(t2.gross_pnl - t2.charges)
    purposes = sorted(o.purpose for o in orders)
    assert purposes == ["ENTRY", "EXIT", "PROTECTIVE_SL"]
    sl = next(o for o in orders if o.purpose == "PROTECTIVE_SL")
    assert sl.status == OrderStatus.CANCELLED.value  # cancelled before the exit order
    assert all(o.tag and o.tag.startswith(f"I{t.instance_id}T{t.id}") for o in orders)
    exit_line = env.messages("EXIT")[-1]
    assert exit_line == "EXIT reason=spot_stop_loss premium_in=100.00 premium_out=80.00 pnl=-6500.00"
    # no re-entry in the same slot after the SL
    env.spot(23700.0)
    await env.settle()
    assert len(env.messages("SIGNAL")) == 1


async def test_timed_exit_at_start_plus_15_minus_2s(env):
    t = await enter_ce(env)
    env.option_price(23650, "CE", 120.0)
    await env.at("09:59", 57)
    assert env.platform.trades.live
    await env.at("09:59", 58)
    await env.wait_for(lambda: not env.platform.trades.live)
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
    assert t2.exit_reason == "timed_exit"
    assert t2.gross_pnl == pytest.approx(20 * 325)


async def test_protective_sl_filled_at_broker(env):
    t = await enter_ce(env)
    await env.wait_for(lambda: env.platform.trades.live[t.id].protective_sl_status == "TRIGGER_PENDING")
    env.option_price(23650, "CE", 55.0)  # premium crashes through the 60 trigger; paper broker fills SL-M
    await env.wait_for(lambda: not env.platform.trades.live)
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
    assert t2.exit_reason == "protective_sl"
    assert t2.exit_price == 55.0


async def test_protective_sl_cancelled_externally_raises_alert(env):
    t = await enter_ce(env)
    await env.wait_for(lambda: env.platform.trades.live[t.id].protective_sl_status == "TRIGGER_PENDING")
    acc = env.platform.accounts[env.account_id]
    paper, _ = acc.shadow()
    await paper.cancel_order(env.platform.trades.live[t.id].protective_sl_order_id)  # user cancels in broker app
    await env.wait_for(lambda: env.types("PROTECTIVE_SL_CANCELLED_EXTERNALLY"))
    assert env.platform.trades.live  # position still managed


async def test_manual_exit_at_broker(env):
    t = await enter_ce(env)
    await env.wait_for(lambda: env.platform.trades.live[t.id].protective_sl_status == "TRIGGER_PENDING")
    acc = env.platform.accounts[env.account_id]
    paper, sync = acc.shadow()
    from brokers.base import OrderRequest

    env.option_price(23650, "CE", 110.0)
    from brokers.base import Exchange

    inst = paper.find_instrument(Exchange.NFO, t.token)
    await paper.place_order(OrderRequest(instrument=inst, side=Side.SELL, quantity=325, order_type=OrderType.MARKET))
    await env.at("09:53", 0)  # let the trade settle past the 5s guard
    for _ in range(3):
        await sync.poll_once(full=True)
        await env.settle(2)
    await env.wait_for(lambda: not env.platform.trades.live)
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
        sl = s.scalar(select(OrderRecord).where(OrderRecord.trade_id == t.id, OrderRecord.purpose == "PROTECTIVE_SL"))
        ext = list(s.scalars(select(OrderRecord).where(OrderRecord.is_external.is_(True))))
    assert t2.exit_reason == "manual_exit_at_broker"
    assert t2.exit_price == 110.0
    assert sl.status == "CANCELLED"  # remaining exit orders cancelled
    assert len(ext) == 1 and env.types("EXTERNAL_ORDER")
    # does not re-enter for that slot
    env.spot(23700)
    await env.settle()
    assert len(env.messages("SIGNAL")) == 1


async def test_buffer_respected_end_to_end(env):
    env.ref_candle("09:45", 23639.20, 23609.20)
    env.spot(23630.0)
    env.option_price(23650, "CE", 100.0)
    await env.at("09:50", 6)
    await env.at("09:51", 0)
    env.spot(23641.40)  # above the high but inside the 2.25 buffer
    await env.settle()
    assert not env.messages("SIGNAL")
    await env.at("10:00", 1)
    assert "SKIP slot=#1 reason=no_breakout" in env.messages("SLOT_SKIPPED")


async def test_disable_exit_now(env):
    t = await enter_ce(env)
    iid = env.instance_ids[0]
    status = await env.platform.strategies.disable(iid, "tester", "exit_now")
    assert status == "STOPPED"
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
        inst = s.get(StrategyInstance, iid)
    assert t2.status == "CLOSED" and t2.exit_reason == "instance_disabled"
    assert not inst.enabled and iid not in env.platform.strategies.runtimes


async def test_disable_finish_trade(env):
    t = await enter_ce(env)
    iid = env.instance_ids[0]
    status = await env.platform.strategies.disable(iid, "tester", "finish_trade")
    assert status == "STOPPING"
    assert env.platform.trades.live  # trade continues
    env.option_price(23650, "CE", 90.0)
    await env.at("10:00", 0)
    await env.wait_for(lambda: iid not in env.platform.strategies.runtimes)
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
        inst = s.get(StrategyInstance, iid)
    assert t2.exit_reason == "timed_exit"
    assert not inst.enabled and inst.status == "STOPPED"


async def test_kill_switch_instance(env):
    t = await enter_ce(env)
    await env.platform.strategies.kill(env.instance_ids[0], "tester")
    with env.db.session() as s:
        t2 = s.get(Trade, t.id)
    assert t2.exit_reason == "kill_switch"
    assert env.types("KILL_SWITCH")


async def test_global_kill_blocks_new_entries(env):
    await env.platform.kill_global("tester")
    assert env.platform.risk.global_kill
    with env.db.session() as s:
        assert not any(i.enabled for i in s.scalars(select(StrategyInstance)))


async def test_param_edit_applies_next_slot(env):
    t = await enter_ce(env)
    params = {"buffer_points": 10, "lots": 2}
    applied = await env.platform.strategies.update_params(env.instance_ids[0], params)
    assert applied is False  # mid-trade: queued
    rt = env.platform.strategies.runtimes[env.instance_ids[0]]
    assert rt.strategy.settings.buffer_points == 2.25
    env.option_price(23650, "CE", 90.0)
    await env.at("10:00", 0)
    await env.wait_for(lambda: not env.platform.trades.live)
    await env.settle()
    assert rt.strategy.settings.buffer_points == 10 and rt.strategy.settings.lots == 2


@register
class _Exploding(Strategy):
    template_id = "test_exploding"
    display_name = "Explodes on timer"
    settings_model = StrategySettings

    def subscriptions(self):
        from brokers.base import Exchange, InstrumentKey, InstrumentType

        return [InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)]

    async def on_timer(self, now):
        raise ValueError("boom")


async def test_instance_isolation(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "GOOD"}, {"name": "BAD", "template": "test_exploding"}])
    try:
        good, bad = env.instance_ids
        await env.at("09:49", 30)
        await env.wait_for(lambda: env.platform.strategies.runtimes[bad].status == "ERROR")
        assert env.types("INSTANCE_ERROR")
        # the good instance keeps working and trades
        t = await enter_ce(env)
        assert t.instance_id == good
        assert env.platform.strategies.runtimes[good].status in ("IN_TRADE", "RUNNING")
    finally:
        await env.platform.stop()


async def test_restart_reconciles_open_trade(tmp_path):
    env = await build_env(tmp_path)
    t = await enter_ce(env)
    await env.wait_for(lambda: env.platform.trades.live[t.id].protective_sl_status == "TRIGGER_PENDING")
    paper_state = env.platform.accounts[env.account_id].paper_shadow.snapshot()
    await env.platform.stop()
    # restart a new platform on the same database
    from storage.repo import save_paper_state
    save_paper_state(env.db, f"shadow:{env.account_id}", paper_state)
    from core.config import AppConfig
    from engine.platform import Platform
    p2 = Platform(AppConfig(data_dir=tmp_path, database_url=env.db.url), env.db, clock=env.clock, db_events=False,
                  file_events=False)
    events = []
    p2.bus.add_sink(events.append)
    await p2.start()
    env2 = Env(platform=p2, db=env.db, clock=env.clock, events=events, account_id=env.account_id,
               instance_ids=env.instance_ids)
    try:
        await env2.wait_for(lambda: p2.accounts[env.account_id].ready.is_set())
        assert t.id in p2.trades.live
        assert env2.types("RESUMED")
        # resumed trade still exits on time
        env2.option_price(23650, "CE", 105.0)
        await env2.at("10:00", 0)
        await env2.wait_for(lambda: not p2.trades.live)
        with env.db.session() as s:
            assert s.get(Trade, t.id).exit_reason == "timed_exit"
    finally:
        await p2.stop()


async def test_no_protective_sl_by_default(tmp_path):
    """protective_sl_pct = 0 (the default): no stop order at the broker; spot SL and timed exit still work."""
    env = await build_env(tmp_path, instances=[{"name": "NO-PSL", "params": {"protective_sl_pct": 0}}])
    try:
        t = await enter_ce(env)
        lt = env.platform.trades.live[t.id]
        assert lt.protective_sl_order_id is None
        with env.db.session() as s:
            purposes = [o.purpose for o in s.scalars(select(OrderRecord).where(OrderRecord.trade_id == t.id))]
        assert "PROTECTIVE_SL" not in purposes
        env.option_price(23650, "CE", 80.0)
        env.spot(23609.20)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            assert s.get(Trade, t.id).exit_reason == "spot_stop_loss"
    finally:
        await env.platform.stop()
