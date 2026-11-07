"""Adapter contract tests (one suite run against every implemented adapter) plus Angel One
mapping tests against a fake SmartConnect, and stub behaviour."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from brokers.angelone import (AngelOneAdapter, AngelOneCredentials, build_order_params, map_fill, map_order,
                              map_position, map_ws_tick, parse_scrip_row)
from brokers.base import (BrokerAdapter, Exchange, InstrumentKey, InstrumentType, OrderModify, OrderRequest,
                          OrderStatus, OrderType, Side)
from brokers.instruments import round_to_tick
from brokers.paper import PaperBroker, PaperCredentials
from brokers.registry import BROKERS, create_adapter, is_stub
from core.clock import FakeClock
from tests.conftest import DAY, NEXT_TUE, at
from tests.mock_broker import SPOT_TOKEN, MockBroker

REQUIRED = ["login", "refresh_session", "is_connected", "get_profile", "get_funds", "load_instruments",
            "resolve_symbol", "place_order", "modify_order", "cancel_order", "get_order_book", "get_positions",
            "get_ltp", "get_historical_candles", "subscribe_ticks", "on_order_update"]


@pytest.mark.parametrize("name", sorted(k for k in BROKERS if k != "mock"))
def test_every_adapter_implements_the_interface(name):
    cls = BROKERS[name]
    assert issubclass(cls, BrokerAdapter)
    for m in REQUIRED:
        assert callable(getattr(cls, m)), m
    assert not getattr(cls, "__abstractmethods__", set()), cls.__abstractmethods__


@pytest.mark.parametrize("name", ["zerodha", "upstox", "dhan", "fyers"])
async def test_stubs_raise_not_implemented(name):
    assert is_stub(name)
    cls = BROKERS[name]
    fields = {k: "x" for k, f in cls.credentials_model.model_fields.items() if f.is_required()}
    a = create_adapter(name, fields)
    with pytest.raises(NotImplementedError):
        await a.login()
    assert a.is_connected() is False


# ------------------------------------------------------------------ contract suite
async def make_mock() -> MockBroker:
    MockBroker.expiries = [DAY, NEXT_TUE]
    b = MockBroker(clock=FakeClock(at("10:00")))
    await b.login()
    return b


async def make_paper_sim() -> PaperBroker:
    b = PaperBroker(PaperCredentials(simulate_market=True), clock=FakeClock(at("10:00")))
    b.market = None
    from brokers.paper import SimulatedMarket

    b.market = SimulatedMarket(b.creds, b.clock, seed=1)
    b._connected = True
    return b


FACTORIES = {"mock": make_mock, "paper_sim": make_paper_sim}


@pytest.fixture(params=list(FACTORIES))
async def adapter(request):
    a = await FACTORIES[request.param]()
    yield a
    await a.close()


def _option(a: BrokerAdapter):
    exp = a.option_expiries("NIFTY")[0]
    spot_key = InstrumentKey(underlying="NIFTY", exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX)
    spot = a.resolve_symbol(spot_key)
    return spot, a.resolve_symbol(InstrumentKey(underlying="NIFTY", exchange=Exchange.NFO, instrument_type=InstrumentType.CE,
                                                expiry=exp, strike=23650.0 if isinstance(a, MockBroker) else
                                                float(round(a.market.spot / 50) * 50)))


def _price(a: BrokerAdapter, token: str, price: float) -> None:
    if isinstance(a, MockBroker):
        a.push(token, price)
    else:
        a._last[(Exchange.NFO.value, token)] = price


async def test_contract_resolve_and_lot_size(adapter):
    spot, opt = _option(adapter)
    assert spot.instrument_type is InstrumentType.INDEX
    assert opt.instrument_type is InstrumentType.CE and opt.lot_size == 65 and opt.tick_size == 0.05
    assert adapter.find_instrument(opt.exchange, opt.token) == opt
    expiries = adapter.option_expiries("NIFTY")
    assert expiries == sorted(expiries)


async def test_contract_market_order_fills_and_updates_stream(adapter):
    updates = []
    adapter.on_order_update(updates.append)
    spot, opt = _option(adapter)
    if isinstance(adapter, MockBroker):
        _price(adapter, opt.token, 101.0)
    oid = await adapter.place_order(OrderRequest(instrument=opt, side=Side.BUY, quantity=130, order_type=OrderType.MARKET,
                                                 tag="I1T1E"))
    await asyncio.sleep(0)
    book = {o.order_id: o for o in await adapter.get_order_book()}
    o = book[oid]
    assert o.status is OrderStatus.COMPLETE and o.filled_quantity == 130 and o.tag == "I1T1E"
    assert any(u.order_id == oid and u.status is OrderStatus.COMPLETE for u in updates)
    pos = {p.token: p for p in await adapter.get_positions()}
    assert pos[opt.token].net_quantity == 130
    fills = await adapter.get_trade_book()
    assert sum(f.quantity for f in fills if f.order_id == oid) == 130


async def test_contract_rejects_non_lot_multiple(adapter):
    _, opt = _option(adapter)
    oid = await adapter.place_order(OrderRequest(instrument=opt, side=Side.BUY, quantity=64, order_type=OrderType.MARKET))
    o = {x.order_id: x for x in await adapter.get_order_book()}[oid]
    assert o.status is OrderStatus.REJECTED


async def test_contract_sl_m_trigger_modify_cancel(adapter):
    _, opt = _option(adapter)
    _price(adapter, opt.token, 100.0)
    if not isinstance(adapter, MockBroker):
        adapter.market = None  # freeze the simulated price so we control the trigger
        adapter._instruments[opt.key] = opt
    buy = await adapter.place_order(OrderRequest(instrument=opt, side=Side.BUY, quantity=65))
    sl = await adapter.place_order(OrderRequest(instrument=opt, side=Side.SELL, quantity=65, order_type=OrderType.SL_M,
                                                trigger_price=60.0))
    book = {o.order_id: o for o in await adapter.get_order_book()}
    assert book[buy].status is OrderStatus.COMPLETE and book[sl].status is OrderStatus.TRIGGER_PENDING
    await adapter.modify_order(OrderModify(order_id=sl, instrument=opt, order_type=OrderType.SL_M, quantity=65,
                                           trigger_price=70.0))
    assert {o.order_id: o for o in await adapter.get_order_book()}[sl].trigger_price == 70.0
    await adapter.cancel_order(sl)
    assert {o.order_id: o for o in await adapter.get_order_book()}[sl].status is OrderStatus.CANCELLED
    sl2 = await adapter.place_order(OrderRequest(instrument=opt, side=Side.SELL, quantity=65, order_type=OrderType.SL_M,
                                                 trigger_price=60.0))
    adapter.on_market_tick(type("T", (), {"exchange": opt.exchange, "token": opt.token, "ltp": 59.0})())  # type: ignore[arg-type]
    o = {x.order_id: x for x in await adapter.get_order_book()}[sl2]
    assert o.status is OrderStatus.COMPLETE and o.average_price == 59.0
    pos = {p.token: p for p in await adapter.get_positions()}[opt.token]
    assert pos.net_quantity == 0 and pos.realized_pnl == pytest.approx((59 - 100) * 65)


async def test_contract_ltp_and_ticks(adapter):
    spot, opt = _option(adapter)
    ticks = []
    adapter.add_tick_listener(ticks.append)
    await adapter.subscribe_ticks([spot])
    if isinstance(adapter, MockBroker):
        adapter.push(SPOT_TOKEN, 23700.0)
        assert ticks and ticks[-1].ltp == 23700.0
    q = await adapter.get_ltp([spot])
    assert q.get(spot.key)


# ------------------------------------------------------------------ Angel One mapping
def test_angel_scrip_master_parsing():
    idx = parse_scrip_row({"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "0.000000",
                           "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0.000000"})
    assert idx.instrument_type is InstrumentType.INDEX and idx.underlying == "NIFTY"
    opt = parse_scrip_row({"token": "43213", "symbol": "NIFTY10NOV2623650PE", "name": "NIFTY", "expiry": "10NOV2026",
                           "strike": "2365000.000000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
                           "tick_size": "5.000000"})
    assert opt.instrument_type is InstrumentType.PE and opt.strike == 23650.0 and opt.lot_size == 65
    assert opt.tick_size == 0.05 and opt.expiry.isoformat() == "2026-11-10"
    assert parse_scrip_row({"exch_seg": "MCX", "instrumenttype": "FUTCOM"}) is None


def test_angel_order_params():
    opt = parse_scrip_row({"token": "43213", "symbol": "NIFTY10NOV2623650CE", "name": "NIFTY", "expiry": "10NOV2026",
                           "strike": "2365000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
                           "tick_size": "5"})
    p = build_order_params(OrderRequest(instrument=opt, side=Side.BUY, quantity=325, tag="I3T42E"))
    assert p["variety"] == "NORMAL" and p["ordertype"] == "MARKET" and p["producttype"] == "INTRADAY"
    assert p["quantity"] == "325" and p["symboltoken"] == "43213" and p["ordertag"] == "I3T42E" and p["exchange"] == "NFO"
    sl = build_order_params(OrderRequest(instrument=opt, side=Side.SELL, quantity=325, order_type=OrderType.SL_M,
                                         trigger_price=60.0))
    assert sl["variety"] == "STOPLOSS" and sl["ordertype"] == "STOPLOSS_MARKET" and sl["triggerprice"] == "60.00"


def test_angel_order_book_and_positions_mapping():
    o = map_order({"orderid": "2611050001", "status": "trigger pending", "transactiontype": "SELL", "ordertype": "STOPLOSS_MARKET",
                   "producttype": "INTRADAY", "exchange": "NFO", "tradingsymbol": "NIFTY10NOV2623650CE", "symboltoken": "43213",
                   "quantity": "325", "filledshares": "0", "unfilledshares": "325", "triggerprice": "60", "ordertag": "I3T42S",
                   "updatetime": "05-Nov-2026 09:52:32", "text": ""})
    assert o.status is OrderStatus.TRIGGER_PENDING and o.order_type is OrderType.SL_M and o.tag == "I3T42S"
    assert o.updated_at.hour == 9 and o.trigger_price == 60.0
    rej = map_order({"orderid": "1", "status": "rejected", "text": "RMS: margin exceeds", "transactiontype": "BUY",
                     "quantity": "65", "exchange": "NFO"})
    assert rej.status is OrderStatus.REJECTED and "margin" in rej.status_message
    part = map_order({"orderid": "2", "status": "open", "quantity": "325", "filledshares": "130", "transactiontype": "BUY"})
    assert part.status is OrderStatus.PARTIALLY_FILLED
    pos = map_position({"tradingsymbol": "NIFTY10NOV2623650CE", "symboltoken": "43213", "exchange": "NFO",
                        "producttype": "INTRADAY", "netqty": "325", "avgnetprice": "100.5", "ltp": "110", "pnl": "3087.5",
                        "realised": "0", "unrealised": "3087.5", "buyqty": "325", "sellqty": "0", "lotsize": "65"})
    assert pos.net_quantity == 325 and pos.average_price == 100.5 and pos.pnl == 3087.5
    f = map_fill({"orderid": "2", "fillid": "F1", "fillprice": "100.5", "fillsize": "325", "filltime": "09:52:31",
                  "transactiontype": "BUY", "tradingsymbol": "X", "symboltoken": "43213", "exchange": "NFO"})
    assert f.quantity == 325 and f.price == 100.5 and f.time.hour == 9
    t = map_ws_tick({"exchange_type": 1, "token": "99926000", "last_traded_price": 2365025, "exchange_timestamp": 1793874151000})
    assert t.exchange is Exchange.NSE and t.ltp == 23650.25


class FakeSmartConnect:
    """Stands in for SmartApi.SmartConnect; records calls and returns canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.expired_once = True

    def getfeedToken(self) -> str:
        return "FEED"

    def generateSession(self, client, pin, totp):
        self.calls.append(("login", (client, pin, len(totp))))
        return {"status": True, "data": {"jwtToken": "Bearer JWT", "refreshToken": "REF", "feedToken": "FEED",
                                         "clientcode": client, "name": "Test"}}

    def placeOrderFullResponse(self, params):
        self.calls.append(("place", params))
        if self.expired_once:
            self.expired_once = False
            return {"status": False, "message": "Invalid Token", "errorcode": "AG8001"}
        return {"status": True, "data": {"orderid": "2611050009", "uniqueorderid": "u"}}

    def orderBook(self):
        return {"status": True, "data": [{"orderid": "2611050009", "status": "complete", "variety": "NORMAL",
                                          "transactiontype": "BUY", "quantity": "65", "filledshares": "65",
                                          "averageprice": "101.5", "exchange": "NFO", "ordertype": "MARKET"}]}

    def cancelOrder(self, oid, variety):
        self.calls.append(("cancel", (oid, variety)))
        return {"status": True, "data": {"orderid": oid}}

    def rmsLimit(self):
        return {"status": True, "data": {"availablecash": "250000.50", "net": "250000.50", "utiliseddebits": "0"}}


async def test_angel_adapter_relogins_on_expired_session(monkeypatch):
    creds = AngelOneCredentials(client_id="A123", api_key="KEY", pin="1234", totp_secret="JBSWY3DPEHPK3PXP")
    a = AngelOneAdapter(creds, account_id=1)
    fake = FakeSmartConnect()
    monkeypatch.setattr(a, "_new_api", lambda: fake)
    prof = await a.login()
    assert prof.client_id == "A123" and a.is_connected()
    assert fake.calls[0][0] == "login" and fake.calls[0][1][2] == 6  # a 6-digit TOTP was generated
    opt = parse_scrip_row({"token": "43213", "symbol": "NIFTY10NOV2623650CE", "name": "NIFTY", "expiry": "10NOV2026",
                           "strike": "2365000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
                           "tick_size": "5"})
    oid = await a.place_order(OrderRequest(instrument=opt, side=Side.BUY, quantity=65, tag="I1T1E"))
    assert oid == "2611050009"
    assert [c[0] for c in fake.calls].count("login") == 2  # expired token → re-login → retried once
    book = await a.get_order_book()
    assert book[0].status is OrderStatus.COMPLETE and book[0].average_price == 101.5
    await a.cancel_order(oid)
    assert fake.calls[-1] == ("cancel", ("2611050009", "NORMAL"))
    funds = await a.get_funds()
    assert funds.available_cash == 250000.50


def test_round_to_tick():
    assert round_to_tick(60.03, 0.05, "down") == 60.0
    assert round_to_tick(60.03, 0.05, "up") == 60.05
    assert round_to_tick(60.03, 0.05) == 60.05
    assert round_to_tick(101.024, 0.05) == 101.0


async def test_simulated_paper_account_fills_at_real_platform_price():
    """A paper account with simulate_market on, trading while a real account is the feed, must fill
    at the real premium the platform has, not at its simulator's price."""
    from brokers.base import Exchange, InstrumentKey, InstrumentType, OrderRequest, OrderType, ProductType, Side
    from brokers.paper import PaperBroker, PaperCredentials

    pb = PaperBroker(PaperCredentials(simulate_market=True, sim_spot_start=24100.0))
    await pb.login()
    try:
        exp = pb.option_expiries("NIFTY")[0]
        inst = pb.resolve_symbol(InstrumentKey(underlying="NIFTY", exchange=Exchange.NFO,
                                               instrument_type=InstrumentType.CE, expiry=exp, strike=24600.0))
        sim_price = pb.market.price_of(inst)
        pb.price_source = lambda exch, tok: 112.5 if tok == inst.token else None  # the real ATM premium
        placed = await pb.place_order(OrderRequest(instrument=inst, side=Side.BUY, quantity=inst.lot_size,
                                                   order_type=OrderType.MARKET, product=ProductType.INTRADAY))
        o = pb.orders[placed if isinstance(placed, str) else placed.order_id]
        assert o.average_price == 112.5 and abs(sim_price - 112.5) > 20
        pb.price_source = lambda exch, tok: None  # platform has no price: simulator is the fallback
        assert pb.last_price(inst.exchange, inst.token) == pb.market.price_of(inst)
    finally:
        await pb.close()
