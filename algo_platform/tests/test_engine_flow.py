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


async def _two_runs_same_strike(tmp_path):
    env = await build_env(tmp_path, instances=[{"name": "A"}, {"name": "B"}])
    env.ref_candle("09:45", 23639.20, 23609.20)
    env.spot(23630.0)
    env.option_price(23650, "CE", 100.0)
    await env.at("09:50", 6)
    await env.at("09:52", 31)
    env.spot(23660.0)
    await env.wait_for(lambda: len(env.platform.trades.live) == 2)
    trades = list(env.platform.trades.live.values())
    assert len({t.token for t in trades}) == 1  # both runs hold the same CE
    await env.wait_for(lambda: all(t.protective_sl_status == "TRIGGER_PENDING" for t in trades))
    paper, sync = env.platform.accounts[env.account_id].shadow()
    return env, trades, paper, sync


async def _sell_at_broker(env, paper, token, qty):
    from brokers.base import Exchange, OrderRequest

    inst = paper.find_instrument(Exchange.NFO, token)
    await paper.place_order(OrderRequest(instrument=inst, side=Side.SELL, quantity=qty, order_type=OrderType.MARKET))


async def test_one_manual_sell_closes_every_run_on_that_strike_at_once(tmp_path):
    env, trades, paper, sync = await _two_runs_same_strike(tmp_path)
    try:
        await env.at("09:53", 0)
        env.option_price(23650, "CE", 110.0)
        await _sell_at_broker(env, paper, trades[0].token, sum(t.open_qty for t in trades))
        await sync.poll_once(full=True)  # the order book shows the manual SELL ...
        await env.settle(2)
        await sync.poll_once(full=True)  # ... one position poll is then enough, for both trades
        await env.settle(2)
        await env.wait_for(lambda: not env.platform.trades.live)
        with env.db.session() as s:
            rows = list(s.scalars(select(Trade).order_by(Trade.id)))
            sl = list(s.scalars(select(OrderRecord).where(OrderRecord.purpose == "PROTECTIVE_SL")))
        assert [r.exit_reason for r in rows] == ["manual_exit_at_broker"] * 2
        assert all(r.exit_price == 110.0 and r.open_quantity == 0 for r in rows)
        assert {o.status for o in sl} == {"CANCELLED"}  # no leftover stop can sell into a short
        assert not env.types("EXIT_FAILED")
    finally:
        await env.platform.stop()


async def test_partial_manual_sell_closes_the_newest_run_only(tmp_path):
    env, trades, paper, sync = await _two_runs_same_strike(tmp_path)
    try:
        await env.at("09:53", 0)
        newest = max(trades, key=lambda t: (t.entry_time, t.id))
        other = next(t for t in trades if t is not newest)
        await _sell_at_broker(env, paper, newest.token, newest.open_qty)
        for _ in range(2):
            await sync.poll_once(full=True)
            await env.settle(2)
        await env.wait_for(lambda: list(env.platform.trades.live) == [other.id])
        assert env.platform.trades.live[other.id].open_qty == other.entry_qty
    finally:
        await env.platform.stop()


async def test_live_exit_never_sells_another_runs_share_of_a_strike(tmp_path):
    """Live: the broker's quantity is shared by every open trade on that strike. If a manual SELL has not
    been reconciled yet, our exit sells only what is left for this trade (never into a short)."""
    env = await build_env(tmp_path, instances=[{"name": "A"}, {"name": "B"}], mode="live")
    try:
        env.ref_candle("09:45", 23639.20, 23609.20)
        env.spot(23630.0)
        env.option_price(23650, "CE", 100.0)
        await env.at("09:50", 6)
        await env.at("09:52", 31)
        env.spot(23660.0)
        await env.wait_for(lambda: len(env.platform.trades.live) == 2)
        acc = env.platform.accounts[env.account_id]
        sync = acc.live_sync
        tm = env.platform.trades
        a, b = sorted(tm.live.values(), key=lambda t: t.id)
        await env.wait_for(lambda: sync.position_for(a.token) is not None
                           and sync.position_for(a.token).net_quantity == a.open_qty + b.open_qty)
        # the broker now holds one trade's quantity (a manual sale the platform has not settled yet)
        sync.position_for(a.token).net_quantity = b.open_qty
        sync.positions_at = env.clock.now()
        sold = []
        real_place = tm.orders.place

        async def spy(adapter, s, req, **kw):
            if kw.get("purpose") == "EXIT":
                sold.append(req.quantity)
            return await real_place(adapter, s, req, **kw)

        tm.orders.place = spy
        await tm.exit_trade(a.id, "manual_test")
        assert sold == [] and env.types("EXIT")  # B's share is not sold; A is settled as a manual exit
        with env.db.session() as s:
            assert s.get(Trade, a.id).exit_reason == "manual_exit_at_broker"
    finally:
        await env.platform.stop()


async def test_pnl_check_ignores_broker_average_price_rounding(tmp_path):
    """Real case (16-Nov, NIFTY 25300 PE, 3 trades x 325): exact fills give -1498.25; the broker rounds its buy /
    sell averages to 2 decimals and shows -1491.75. That is not a mismatch. A real gap is logged once."""
    from brokers.base import Exchange, Position, ProductType

    env = await build_env(tmp_path)
    try:
        p = env.platform
        today = p.clock.now().date()
        with env.db.session() as s:
            for ref, g in (("R1", -2483.0), ("R2", -2486.25), ("R3", 3471.0)):
                s.add(Trade(trade_ref=ref, account_id=env.account_id, mode="paper", trade_date=today, token="T1",
                            direction="PE_BUY", status="CLOSED", gross_pnl=g))
            s.commit()
        _, sync = p.accounts[env.account_id].shadow()

        def pos(pnl):
            return Position(exchange=Exchange.NFO, tradingsymbol="NIFTY17NOV2625300PE", token="T1",
                            product=ProductType.INTRADAY, net_quantity=0, buy_quantity=975, sell_quantity=975,
                            buy_average=103.01, sell_average=101.48, pnl=pnl)

        p.trades._reconcile_pnl(sync, [pos(-1491.75)])
        assert not env.types("PNL_MISMATCH")
        for _ in range(3):  # a real gap (a missing fill): reported, but once, not every poll
            p.trades._reconcile_pnl(sync, [pos(-1000.0)])
        assert len(env.types("PNL_MISMATCH")) == 1
    finally:
        await p.stop()


async def test_booked_tp_profit_counts_in_pnl_check_limits_and_totals(tmp_path):
    """Real case (16-Nov, paper): TP1 sold part of an open trade; the broker shows that profit as realised at once.
    The platform left it out until the trade closed -> PNL_MISMATCH -7943 and too-low daily P&L for the limits."""
    from types import SimpleNamespace

    from brokers.base import Exchange, OrderStatus, Position, ProductType

    env = await build_env(tmp_path)
    try:
        p = env.platform
        tm = p.trades
        _, sync = p.accounts[env.account_id].shadow()
        lt = SimpleNamespace(id=900, ref="T900", token="T9", account_id=env.account_id, mode="paper", instance_id=None,
                             status="OPEN", partial_busy=False, entry_price=80.0, open_qty=390, entry_qty=650,
                             option_series="X", partials=[{"label": "TP1", "filled": 260, "price": 100.0}])
        tm.live[900] = lt
        assert tm.booked(lt) == 260 * 20.0
        # broker: 260 sold at 100 (realised +5200) + 390 still held at ltp 90 (+3900)
        pos = Position(exchange=Exchange.NFO, tradingsymbol="NIFTY25250PE", token="T9", product=ProductType.INTRADAY,
                       net_quantity=390, buy_quantity=650, sell_quantity=260, ltp=90.0, pnl=9100.0)
        tm._reconcile_pnl(sync, [pos])
        assert not env.types("PNL_MISMATCH") and tm.recon[(env.account_id, True)]["T9"] == 0
        p.market.ltp = lambda key: 90.0
        assert tm.open_pnl(lt) == 9100.0 and tm.daily_pnl(p.clock.now().date()) == 9100.0  # limits see it

        # an order of ours filling on that strike: the broker is a moment ahead -> no alarm
        sync.orders["X1"] = SimpleNamespace(token="T9", status=OrderStatus.OPEN, exchange_time=None, placed_at=None)
        tm._reconcile_pnl(sync, [pos.model_copy(update={"pnl": 0.0})])
        assert not env.types("PNL_MISMATCH")
        # a resting protective stop does not switch the check off
        sync.orders["X1"].status = OrderStatus.TRIGGER_PENDING
        tm._reconcile_pnl(sync, [pos.model_copy(update={"pnl": 0.0})])
        assert len(env.types("PNL_MISMATCH")) == 1
        del sync.orders["X1"]
        tm.live.pop(900)
    finally:
        await p.stop()


async def _live_trade(tmp_path):
    env = await build_env(tmp_path, mode="live")
    env.ref_candle("09:45", 23639.20, 23609.20)
    env.spot(23630.0)
    env.option_price(23650, "CE", 100.0)
    await env.at("09:50", 6)
    await env.at("09:52", 31)
    env.spot(23660.0)
    await env.wait_for(lambda: env.platform.trades.live)
    lt = next(iter(env.platform.trades.live.values()))
    sync = env.platform.accounts[env.account_id].live_sync
    await env.wait_for(lambda: lt.protective_sl_status == "TRIGGER_PENDING")
    return env, lt, sync


def _spy_exits(tm):
    sent = []
    real = tm.orders.place

    async def spy(adapter, s, req, **kw):
        if kw.get("purpose") == "EXIT":
            sent.append(req.quantity)
        return await real(adapter, s, req, **kw)

    tm.orders.place = spy
    return sent


async def test_manual_stop_at_broker_blocks_our_exit_without_order_spam(tmp_path):
    """Real case (17-Nov, Ram): a manual SL SELL placed in the broker's app held the whole position; every exit sell
    of ours was a short sale to Angel (rejected for funds) -> 111 rejected orders. Now: no order, one clear alert,
    and the exit goes through once the manual order is cancelled."""
    from brokers.base import Exchange, OrderRequest

    env, lt, sync = await _live_trade(tmp_path)
    try:
        tm = env.platform.trades
        inst = env.mock.find_instrument(Exchange.NFO, lt.token)
        manual = await env.mock.place_order(OrderRequest(instrument=inst, side=Side.SELL, quantity=lt.open_qty,
                                                         order_type=OrderType.SL_M, trigger_price=80.0))
        await sync.poll_once(full=True)
        await env.settle()
        sent = _spy_exits(tm)
        assert await tm.exit_trade(lt.id, "timed_exit") is False
        assert await tm.exit_trade(lt.id, "timed_exit") is False  # the re-check: still blocked, still quiet
        assert sent == [] and lt.id in tm.live and lt.status == "OPEN"
        msgs = env.messages("EXIT_BLOCKED_BY_MANUAL_ORDER")
        assert len(msgs) == 1 and manual in msgs[0] and "trigger 80" in msgs[0]
        await env.mock.cancel_order(manual)  # the user cancels the manual stop
        await sync.poll_once(full=True)
        await env.settle()
        assert await tm.exit_trade(lt.id, "timed_exit") is True
        assert sent == [325] and not tm.live
        with env.db.session() as s:
            assert s.get(Trade, lt.id).exit_reason == "timed_exit"
    finally:
        await env.platform.stop()


async def test_funds_rejected_exit_backs_off_instead_of_three_orders_every_5s(tmp_path):
    env, lt, sync = await _live_trade(tmp_path)
    try:
        tm = env.platform.trades
        real_place = env.mock.place_order

        async def refuse_sells(req):
            if req.side is Side.SELL and req.order_type is OrderType.MARKET:
                o = env.mock._new_order(req, OrderStatus.REJECTED, "Your order has been rejected due to Insufficient "
                                                                  "Funds. Available funds - Rs. 203501.55.")
                env.mock._publish(o)
                return o.order_id
            return await real_place(req)

        env.mock.place_order = refuse_sells
        sent = _spy_exits(tm)
        assert await tm.exit_trade(lt.id, "timed_exit") is False
        assert sent == [325]  # one order, not three in a row
        assert "retrying in 5s" in env.messages("EXIT_FAILED")[-1] and "Insufficient Funds" in env.messages("EXIT_FAILED")[-1]
        assert await tm.exit_trade(lt.id, "timed_exit") is False
        assert sent == [325, 325] and "retrying in 10s" in env.messages("EXIT_FAILED")[-1]
        env.mock.place_order = real_place
        assert await tm.exit_trade(lt.id, "timed_exit") is True and not tm.live
    finally:
        await env.platform.stop()
