"""Paper broker: simulated fills on live prices.

Two ways to use it:
1. As the paper "shadow" of a real account: an instance in paper mode routes its orders here.
   Instruments, lot sizes and historical candles come from the real account (the delegate)
   and prices come from the shared market-data service.
2. As a standalone "Paper" broker account. With `simulate_market` on it also generates a
   synthetic NIFTY (and SENSEX) feed + option chains (Black-Scholes premiums) so the whole platform
   can be exercised without any broker credentials.

Fill model: MARKET fills at the last traded price (+/- slippage ticks); LIMIT fills when the
price trades through the limit; SL-M triggers when the price touches the trigger and fills at
market; SL (limit) triggers, then behaves like a LIMIT. Quantities must be a multiple of the
lot size, as on the exchange.
"""
from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, ClassVar

from pydantic import Field

from brokers.base import (
    BrokerAdapter,
    BrokerCredentials,
    BrokerError,
    Candle,
    Exchange,
    Fill,
    Funds,
    Instrument,
    InstrumentKey,
    InstrumentType,
    Order,
    OrderModify,
    OrderRejected,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    ProductType,
    Profile,
    Side,
    Tick,
)
from brokers.instruments import InstrumentStore, round_to_tick
from core.clock import Clock, floor_to_timeframe, now_ist
from core.options_math import next_weekly_expiries, option_premium

SIM_NIFTY_TOKEN = "99926000"   # same token Angel One uses for the NIFTY 50 index
SIM_SENSEX_TOKEN = "99919000"  # same token Angel One uses for the SENSEX index


class PaperCredentials(BrokerCredentials):
    starting_capital: float = Field(500000.0, ge=0, title="Starting capital (₹)")
    slippage_ticks: int = Field(0, ge=0, le=50, title="Slippage (ticks) on market fills")
    simulate_market: bool = Field(False, title="Simulate market data",
                                  description="Generate synthetic NIFTY and SENSEX feeds and option chains "
                                              "(demo/testing only)")
    sim_spot_start: float = Field(24000.0, gt=0, title="Simulated NIFTY start")
    sim_points_per_tick: float = Field(1.2, gt=0, title="Simulated NIFTY volatility (points per tick)")
    sim_iv: float = Field(0.14, gt=0, lt=2, title="Simulated option IV")
    sim_lot_size: int = Field(65, ge=1, title="Simulated NIFTY lot size")
    sim_sensex: bool = Field(True, title="Also simulate SENSEX (BSE index, BFO options)")
    sim_sensex_start: float = Field(79000.0, gt=0, title="Simulated SENSEX start")
    sim_sensex_lot_size: int = Field(20, ge=1, title="Simulated SENSEX lot size")
    sim_tick_interval_sec: float = Field(0.5, gt=0.05, title="Simulated tick interval (s)")


@dataclass
class _SimIndex:
    name: str
    exchange: Exchange
    deriv: Exchange
    token: str
    symbol: str
    strike_step: int
    lot_size: int
    expiry_weekday: int
    scale: float  # volatility relative to NIFTY
    spot: float
    bars: dict[datetime, list[float]] = field(default_factory=dict)


class SimulatedMarket:
    """Synthetic index random walks (NIFTY, optionally SENSEX) + weekly option chains (Black-Scholes)."""

    def __init__(self, creds: PaperCredentials, clock: Clock | None = None, seed: int | None = None):
        self.creds = creds
        self.clock = clock or Clock()
        self.rng = random.Random(seed)
        self.store = InstrumentStore()
        self.indices: dict[str, _SimIndex] = {
            SIM_NIFTY_TOKEN: _SimIndex("NIFTY", Exchange.NSE, Exchange.NFO, SIM_NIFTY_TOKEN, "Nifty 50", 50,
                                       creds.sim_lot_size, 1, 1.0, creds.sim_spot_start)}
        if creds.sim_sensex:
            self.indices[SIM_SENSEX_TOKEN] = _SimIndex(
                "SENSEX", Exchange.BSE, Exchange.BFO, SIM_SENSEX_TOKEN, "SENSEX", 100, creds.sim_sensex_lot_size, 3,
                creds.sim_sensex_start / creds.sim_spot_start, creds.sim_sensex_start)
        self._tokens = itertools.count(900001)
        for ix in self.indices.values():
            self.store.add(Instrument(exchange=ix.exchange, token=ix.token, tradingsymbol=ix.symbol, underlying=ix.name,
                                      instrument_type=InstrumentType.INDEX, lot_size=1, tick_size=0.05))
            self._build_chain(ix)
        self.index = self.store.by_token(Exchange.NSE, SIM_NIFTY_TOKEN)
        self._opt_index: dict[str, _SimIndex] = {}
        for inst in self.store.all():
            if inst.instrument_type in (InstrumentType.CE, InstrumentType.PE):
                self._opt_index[inst.token] = next(i for i in self.indices.values() if i.name == inst.underlying)

    @property
    def spot(self) -> float:
        return self.indices[SIM_NIFTY_TOKEN].spot

    @property
    def minute_bars(self) -> dict[datetime, list[float]]:
        return self.indices[SIM_NIFTY_TOKEN].bars

    def _build_chain(self, ix: _SimIndex) -> None:
        today = self.clock.now().date()
        step = ix.strike_step
        centre = round(ix.spot / step) * step
        width = 40 * step
        for exp in next_weekly_expiries(today, weekday=ix.expiry_weekday, count=4):
            for k in range(int(centre - width), int(centre + width) + 1, step):
                for typ in (InstrumentType.CE, InstrumentType.PE):
                    sym = f"{ix.name}{exp:%d%b%y}{k}{typ.value}".upper()
                    self.store.add(Instrument(exchange=ix.deriv, token=str(next(self._tokens)), tradingsymbol=sym,
                                              underlying=ix.name, instrument_type=typ, expiry=exp, strike=float(k),
                                              lot_size=ix.lot_size, tick_size=0.05))

    def step(self) -> float:
        drift = self.rng.gauss(0.0, self.creds.sim_points_per_tick)
        # occasional bursts so breakouts actually happen in a demo
        if self.rng.random() < 0.02:
            drift *= 6
        now = self.clock.now()
        for ix in self.indices.values():
            own = 0.0 if ix.scale == 1.0 else self.rng.gauss(0.0, self.creds.sim_points_per_tick * ix.scale * 0.4)
            ix.spot = max(1000.0, round(ix.spot + drift * ix.scale + own, 2))
            self._record(ix, now, ix.spot)
        return self.spot

    def _record(self, ix: _SimIndex, ts: datetime, price: float) -> None:
        m = floor_to_timeframe(ts, 1)
        bar = ix.bars.get(m)
        if bar is None:
            ix.bars[m] = [price, price, price, price]
            cutoff = m - timedelta(days=3)
            for old in [k for k in ix.bars if k < cutoff]:
                del ix.bars[old]
        else:
            bar[1] = max(bar[1], price)
            bar[2] = min(bar[2], price)
            bar[3] = price

    def is_index(self, token: str) -> bool:
        return token in self.indices

    def price_of(self, inst: Instrument) -> float:
        ix = self.indices.get(inst.token)
        if ix is not None:
            return ix.spot
        if inst.instrument_type in (InstrumentType.CE, InstrumentType.PE) and inst.expiry and inst.strike:
            under = self._opt_index.get(inst.token)
            spot = under.spot if under else self.spot
            return option_premium(spot, inst.strike, inst.expiry, self.clock.now(), self.creds.sim_iv,
                                  inst.instrument_type is InstrumentType.CE)
        return 0.0

    def candles(self, timeframe_min: int, start: datetime, end: datetime, token: str = SIM_NIFTY_TOKEN) -> list[Candle]:
        ix = self.indices.get(token)
        if ix is None:
            return []
        buckets: dict[datetime, list[float]] = {}
        for m in sorted(ix.bars):
            if m < start or m > end:
                continue
            o, h, l, c = ix.bars[m]
            b = floor_to_timeframe(m, timeframe_min)
            cur = buckets.get(b)
            if cur is None:
                buckets[b] = [o, h, l, c]
            else:
                cur[1], cur[2], cur[3] = max(cur[1], h), min(cur[2], l), c
        return [Candle(start=k, timeframe_min=timeframe_min, open=v[0], high=v[1], low=v[2], close=v[3],
                       source="historical") for k, v in sorted(buckets.items())]


class PaperBroker(BrokerAdapter):
    broker_type: ClassVar[str] = "paper"
    display_name: ClassVar[str] = "Paper trading"
    credentials_model: ClassVar[type[BrokerCredentials]] = PaperCredentials
    supports_order_stream: ClassVar[bool] = True
    supports_tick_stream: ClassVar[bool] = True
    is_paper: ClassVar[bool] = True
    order_tag_max_len: ClassVar[int] = 40

    def __init__(self, credentials: PaperCredentials | None = None, *, delegate: BrokerAdapter | None = None,
                 clock: Clock | None = None, price_source: Callable[[Exchange, str], float | None] | None = None,
                 **kw: Any) -> None:
        super().__init__(credentials or PaperCredentials(), **kw)
        self.creds: PaperCredentials = self.credentials  # type: ignore[assignment]
        self.delegate = delegate
        self.clock = clock or Clock()
        self.price_source = price_source
        self.market: SimulatedMarket | None = None
        self._connected = False
        self._ids = itertools.count(1)
        self._fill_ids = itertools.count(1)
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self._positions: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._instruments: dict[tuple[str, str], Instrument] = {}
        self._last: dict[tuple[str, str], float] = {}
        self._sim_task: asyncio.Task | None = None
        self._subscribed: set[tuple[str, str]] = set()
        self.state_listeners: list[Callable[[], None]] = []
        self.cash = self.creds.starting_capital

    # ------------------------------------------------------------------ session
    async def login(self) -> Profile:
        if self.creds.simulate_market and self.market is None:
            self.market = SimulatedMarket(self.creds, self.clock)
        self._connected = True
        self.last_login_at = now_ist()
        if self.market is not None and self._sim_task is None:
            try:
                self._sim_task = asyncio.get_running_loop().create_task(self._run_simulation())
            except RuntimeError:
                pass
        self._emit_connection("orders", "connected", "paper")
        return Profile(client_id=f"PAPER-{self.account_id or 0}", name=self.label or "Paper", broker="paper")

    async def refresh_session(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    @property
    def simulated_feed(self) -> bool:
        """True when this account's prices are synthetic (simulate_market on)."""
        return self.market is not None

    async def close(self) -> None:
        if self._sim_task:
            self._sim_task.cancel()
            self._sim_task = None
        self._connected = False

    async def get_profile(self) -> Profile:
        return Profile(client_id=f"PAPER-{self.account_id or 0}", name=self.label or "Paper", broker="paper")

    async def get_funds(self) -> Funds:
        used = sum(abs(p["net"]) * p["avg"] for p in self._positions.values())
        return Funds(available_cash=round(self.cash, 2), used_margin=round(used, 2),
                     net=round(self.cash + used, 2))

    # ------------------------------------------------------------------ instruments
    async def load_instruments(self, force: bool = False) -> int:
        if self.market is not None:
            return len(self.market.store)
        if self.delegate is not None and force:
            if self.delegate.is_connected():
                return await self.delegate.load_instruments(force)
        return 0

    def _store_resolve(self, key: InstrumentKey) -> Instrument:
        if self.market is not None:
            return self.market.store.resolve(key)
        if self.delegate is not None:
            return self.delegate.resolve_symbol(key)
        raise BrokerError("paper broker has no instrument source (enable simulate_market or link a real account)")

    def resolve_symbol(self, key: InstrumentKey) -> Instrument:
        inst = self._store_resolve(key)
        self._instruments[inst.key] = inst
        return inst

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        if self.market is not None:
            return self.market.store.option_expiries(underlying, exchange)
        if self.delegate is not None:
            return self.delegate.option_expiries(underlying, exchange)
        return []

    def find_instrument(self, exchange: Exchange, token: str) -> Instrument | None:
        inst = self._instruments.get((exchange.value, token))
        if inst:
            return inst
        if self.market is not None:
            return self.market.store.by_token(exchange, token)
        if self.delegate is not None:
            return self.delegate.find_instrument(exchange, token)
        return None

    # ------------------------------------------------------------------ prices
    def last_price(self, exchange: Exchange, token: str) -> float | None:
        """Real platform prices win: when another account is the data feed, a simulated account must
        fill at the market's real premium, not at its own simulator's (06-Nov-2026: the simulator's
        NIFTY sat ~500 points away, so "ATM" fills were deep ITM/OTM). The simulator is only used when
        the platform has no price for the instrument, e.g. when this account is itself the feed."""
        key = (exchange.value, token)
        if self.price_source is not None:
            p = self.price_source(exchange, token)
            if p:
                self._last[key] = p
                return p
        if self.market is not None:
            inst = self.find_instrument(exchange, token)
            if inst is not None:
                return self.market.price_of(inst)
        return self._last.get(key)

    def on_market_tick(self, tick: Tick) -> None:
        """Feed a live price into the simulator (called by the market-data service)."""
        self._last[(tick.exchange.value, tick.token)] = tick.ltp
        self._match_working(tick.exchange, tick.token, tick.ltp)

    async def get_ltp(self, instruments: list[Instrument]) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        missing: list[Instrument] = []
        for inst in instruments:
            p = self.last_price(inst.exchange, inst.token)
            if p:
                out[inst.key] = p
            else:
                missing.append(inst)
        if missing and self.delegate is not None and self.delegate.is_connected():
            out.update(await self.delegate.get_ltp(missing))
        return out

    async def get_historical_candles(self, instrument: Instrument, timeframe_min: int, start: datetime,
                                     end: datetime) -> list[Candle]:
        if self.market is not None and self.market.is_index(instrument.token):
            return self.market.candles(timeframe_min, start, end, instrument.token)
        if self.delegate is not None and self.delegate.is_connected():
            return await self.delegate.get_historical_candles(instrument, timeframe_min, start, end)
        return []

    async def subscribe_ticks(self, instruments: list[Instrument]) -> None:
        for inst in instruments:
            self._subscribed.add(inst.key)
            self._instruments.setdefault(inst.key, inst)

    async def unsubscribe_ticks(self, instruments: list[Instrument]) -> None:
        for inst in instruments:
            self._subscribed.discard(inst.key)

    async def _run_simulation(self) -> None:
        assert self.market is not None
        self._emit_connection("ticks", "connected", "simulated")
        try:
            while True:
                self.market.step()
                now = self.clock.now()
                for key in list(self._subscribed):
                    inst = self.find_instrument(Exchange(key[0]), key[1])
                    if inst is None:
                        continue
                    price = self.market.price_of(inst)
                    self._last[key] = price
                    self._emit_tick(Tick(exchange=inst.exchange, token=inst.token, ltp=price, ts=now))
                # working orders on unsubscribed instruments still need matching
                for o in list(self.orders.values()):
                    if o.status.is_working and (o.exchange.value, o.token) not in self._subscribed:
                        p = self.last_price(o.exchange, o.token)
                        if p:
                            self._match_working(o.exchange, o.token, p)
                await asyncio.sleep(self.creds.sim_tick_interval_sec)
        except asyncio.CancelledError:
            self._emit_connection("ticks", "disconnected", "simulated")
            raise

    # ------------------------------------------------------------------ orders
    def _new_order(self, req: OrderRequest, status: OrderStatus, msg: str = "") -> Order:
        oid = f"P{now_ist():%y%m%d}{next(self._ids):05d}"
        o = Order(order_id=oid, exchange=req.instrument.exchange, tradingsymbol=req.instrument.tradingsymbol,
                  token=req.instrument.token, side=req.side, quantity=req.quantity, pending_quantity=req.quantity,
                  price=req.price, trigger_price=req.trigger_price, order_type=req.order_type, product=req.product,
                  validity=req.validity, status=status, status_message=msg, tag=req.tag, placed_at=self.clock.now(),
                  updated_at=self.clock.now(), exchange_order_id=f"X{oid}")
        self.orders[oid] = o
        self._instruments.setdefault(req.instrument.key, req.instrument)
        return o

    async def place_order(self, req: OrderRequest) -> str:
        if not self._connected:
            raise BrokerError("paper broker not logged in", retryable=True)
        lot = max(1, req.instrument.lot_size)
        if req.quantity <= 0 or req.quantity % lot:
            o = self._new_order(req, OrderStatus.REJECTED, f"quantity {req.quantity} is not a multiple of lot {lot}")
            self._publish(o)
            return o.order_id
        if req.order_type in (OrderType.LIMIT, OrderType.SL) and not req.price:
            raise OrderRejected("limit price required")
        if req.order_type in (OrderType.SL, OrderType.SL_M) and not req.trigger_price:
            raise OrderRejected("trigger price required")
        status = OrderStatus.TRIGGER_PENDING if req.order_type in (OrderType.SL, OrderType.SL_M) else OrderStatus.OPEN
        o = self._new_order(req, status)
        self._publish(o)
        ltp = self.last_price(req.instrument.exchange, req.instrument.token)
        if req.order_type is OrderType.MARKET and not ltp:
            o.status, o.status_message, o.pending_quantity = OrderStatus.REJECTED, "no market price available", 0
            self._publish(o)
            return o.order_id
        if req.order_type is OrderType.SL_M and ltp:
            # Exchange rule: a sell stop must be below LTP, a buy stop above it.
            bad = (o.side is Side.SELL and req.trigger_price >= ltp) or (o.side is Side.BUY and req.trigger_price <= ltp)
            if bad:
                o.status, o.pending_quantity = OrderStatus.REJECTED, 0
                o.status_message = f"trigger {req.trigger_price} invalid vs LTP {ltp}"
                self._publish(o)
                return o.order_id
        if ltp:
            self._try_fill(o, ltp)
        return o.order_id

    async def modify_order(self, req: OrderModify) -> str:
        o = self.orders.get(req.order_id)
        if o is None:
            raise BrokerError(f"unknown order {req.order_id}")
        if not o.status.is_working:
            raise BrokerError(f"order {req.order_id} is {o.status.value}; cannot modify")
        o.order_type, o.quantity = req.order_type, req.quantity
        o.pending_quantity = req.quantity - o.filled_quantity
        o.price, o.trigger_price = req.price, req.trigger_price
        if req.order_type in (OrderType.MARKET, OrderType.LIMIT) and o.status is OrderStatus.TRIGGER_PENDING:
            o.status = OrderStatus.OPEN
        o.updated_at = self.clock.now()
        self._publish(o)
        ltp = self.last_price(o.exchange, o.token)
        if ltp:
            self._try_fill(o, ltp)
        return o.order_id

    async def cancel_order(self, order_id: str) -> str:
        o = self.orders.get(order_id)
        if o is None:
            raise BrokerError(f"unknown order {order_id}")
        if not o.status.is_working:
            raise BrokerError(f"order {order_id} is {o.status.value}; cannot cancel")
        o.status, o.pending_quantity, o.updated_at = OrderStatus.CANCELLED, 0, self.clock.now()
        self._publish(o)
        return order_id

    def _match_working(self, exchange: Exchange, token: str, ltp: float) -> None:
        for o in list(self.orders.values()):
            if o.status.is_working and o.token == token and o.exchange == exchange:
                self._try_fill(o, ltp)

    def _try_fill(self, o: Order, ltp: float) -> None:
        if o.status is OrderStatus.TRIGGER_PENDING:
            trig = o.trigger_price or 0.0
            hit = ltp <= trig if o.side is Side.SELL else ltp >= trig
            if not hit:
                return
            if o.order_type is OrderType.SL_M:
                self._fill(o, ltp)
            else:
                o.status = OrderStatus.OPEN
                o.updated_at = self.clock.now()
                self._publish(o)
                self._try_fill(o, ltp)
            return
        if o.order_type in (OrderType.MARKET, OrderType.SL_M):
            self._fill(o, ltp)
        elif o.price is not None:
            marketable = ltp <= o.price if o.side is Side.BUY else ltp >= o.price
            if marketable:
                # a marketable limit fills at the better of LTP and the limit
                self._fill(o, min(ltp, o.price) if o.side is Side.BUY else max(ltp, o.price), slip=False)

    def _fill(self, o: Order, price: float, slip: bool = True) -> None:
        inst = self.find_instrument(o.exchange, o.token)
        tick = inst.tick_size if inst else 0.05
        if slip and self.creds.slippage_ticks:
            price += (tick * self.creds.slippage_ticks) * (1 if o.side is Side.BUY else -1)
        price = max(tick, round_to_tick(price, tick))
        qty = o.pending_quantity or (o.quantity - o.filled_quantity)
        now = self.clock.now()
        total = o.average_price * o.filled_quantity + price * qty
        o.filled_quantity += qty
        o.average_price = round(total / o.filled_quantity, 4)
        o.pending_quantity = 0
        o.status = OrderStatus.COMPLETE
        o.updated_at = o.exchange_time = now
        self.fills.append(Fill(fill_id=f"F{next(self._fill_ids):06d}", order_id=o.order_id, exchange=o.exchange,
                               tradingsymbol=o.tradingsymbol, token=o.token, side=o.side, price=price, quantity=qty,
                               time=now, product=o.product))
        self._apply_position(o, price, qty, inst)
        self._publish(o)

    def _apply_position(self, o: Order, price: float, qty: int, inst: Instrument | None) -> None:
        key = (o.exchange.value, o.token, o.product.value)
        p = self._positions.setdefault(key, {"net": 0, "avg": 0.0, "buy_qty": 0, "sell_qty": 0, "buy_val": 0.0,
                                             "sell_val": 0.0, "realized": 0.0, "symbol": o.tradingsymbol,
                                             "lot": inst.lot_size if inst else 1})
        signed = qty if o.side is Side.BUY else -qty
        if o.side is Side.BUY:
            p["buy_qty"] += qty
            p["buy_val"] += qty * price
            self.cash -= qty * price
        else:
            p["sell_qty"] += qty
            p["sell_val"] += qty * price
            self.cash += qty * price
        net, avg = p["net"], p["avg"]
        if net == 0 or (net > 0) == (signed > 0):
            new_net = net + signed
            p["avg"] = (abs(net) * avg + qty * price) / abs(new_net)
            p["net"] = new_net
        else:
            closing = min(abs(net), qty)
            p["realized"] += closing * (price - avg) * (1 if net > 0 else -1)
            new_net = net + signed
            p["net"] = new_net
            if new_net == 0:
                p["avg"] = 0.0
            elif (new_net > 0) != (net > 0):
                p["avg"] = price

    async def get_order_book(self) -> list[Order]:
        return [o.model_copy() for o in self.orders.values()]

    async def get_trade_book(self) -> list[Fill]:
        return list(self.fills)

    async def get_positions(self) -> list[Position]:
        out = []
        for (exch, token, product), p in self._positions.items():
            ltp = self.last_price(Exchange(exch), token) or p["avg"]
            unreal = (ltp - p["avg"]) * p["net"] if p["net"] else 0.0
            out.append(Position(
                exchange=Exchange(exch), tradingsymbol=p["symbol"], token=token, product=ProductType(product),
                net_quantity=p["net"], average_price=round(p["avg"], 4), buy_quantity=p["buy_qty"],
                sell_quantity=p["sell_qty"],
                buy_average=round(p["buy_val"] / p["buy_qty"], 4) if p["buy_qty"] else 0.0,
                sell_average=round(p["sell_val"] / p["sell_qty"], 4) if p["sell_qty"] else 0.0,
                realized_pnl=round(p["realized"], 2), unrealized_pnl=round(unreal, 2),
                pnl=round(p["realized"] + unreal, 2), ltp=ltp, lot_size=p["lot"]))
        return out

    def _publish(self, o: Order) -> None:
        snapshot = o.model_copy()
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(self._emit_order, snapshot)
        except RuntimeError:
            self._emit_order(snapshot)
        for cb in list(self.state_listeners):
            try:
                cb()
            except Exception:
                pass

    # ------------------------------------------------------------------ persistence
    def snapshot(self) -> dict[str, Any]:
        return {
            "date": self.clock.now().date().isoformat(),
            "cash": self.cash,
            "orders": [o.model_dump(mode="json", exclude={"raw"}) for o in self.orders.values()],
            "fills": [f.model_dump(mode="json") for f in self.fills],
            "positions": [{"key": list(k), **v} for k, v in self._positions.items()],
            "instruments": [i.model_dump(mode="json") for i in self._instruments.values()],
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Restore state saved earlier today (intraday paper positions do not carry over days)."""
        self.cash = float(state.get("cash", self.creds.starting_capital))
        if state.get("date") != self.clock.now().date().isoformat():
            return
        for raw in state.get("instruments", []):
            inst = Instrument.model_validate(raw)
            self._instruments[inst.key] = inst
        for raw in state.get("orders", []):
            o = Order.model_validate(raw)
            self.orders[o.order_id] = o
        self.fills = [Fill.model_validate(f) for f in state.get("fills", [])]
        for p in state.get("positions", []):
            key = tuple(p.pop("key"))
            self._positions[key] = p  # type: ignore[index]
        nums = [int(o[-5:]) for o in self.orders if o[-5:].isdigit()]
        self._ids = itertools.count((max(nums) + 1) if nums else 1)
        self._fill_ids = itertools.count(len(self.fills) + 1)

