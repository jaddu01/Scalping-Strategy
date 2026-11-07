"""Trade lifecycle: entry → fill confirmation → protective SL → exit → P&L, plus broker-driven
events (protective SL hit/cancelled at the broker, manual exits), restart reconciliation,
live P&L / reconciliation against the broker, and kill switches."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import func, select

from brokers.base import (Exchange, InstrumentKey, InstrumentType, Order, OrderRequest, OrderStatus, OrderType,
                          Position, ProductType, Side)
from brokers.instruments import round_to_tick
from core.clock import Clock, combine, fmt_hm, format_duration
from core.events import Category, EventBus, Level
from core.settings_schema import GlobalSettings
from engine.accounts import AccountRuntime, BrokerSync, make_tag, parse_tag
from engine.charges import option_charges
from engine.exit_manager import ExitManager, ExitState
from engine.market_data import MarketDataService, series_of
from engine.order_manager import OrderManager
from engine.risk_manager import RiskManager
from storage.db import Database
from storage.models import OrderRecord, Trade
from storage.repo import next_trade_ref
from strategies.base import EntrySignal, TradeInfo

if TYPE_CHECKING:
    from engine.strategy_manager import InstanceRuntime

log = logging.getLogger("algo.trades")

ACTIVE = ("PENDING_ENTRY", "OPEN", "EXITING")


@dataclass
class LiveTrade:
    id: int
    ref: str
    instance_id: int | None
    instance_name: str
    account_id: int
    account_label: str
    mode: str
    direction: str
    slot_id: str
    symbol: str
    token: str
    exchange: Exchange
    option_key: InstrumentKey
    spot_key: InstrumentKey | None
    lots: int
    lot_size: int
    tick_size: float
    open_qty: int
    entry_qty: int
    entry_price: float
    entry_time: datetime | None
    exit_at: datetime
    spot_sl: float | None
    spot_sl_side: str
    tp_spot_level: float | None
    tp_spot_side: str
    tp_premium_level: float | None
    protective_pct: float | None
    protective_sl_order_id: str | None = None
    protective_sl_trigger: float | None = None
    protective_sl_status: str | None = None
    status: str = "OPEN"
    exit_order_ids: list[str] = field(default_factory=list)
    cancelling_sl: bool = False
    missing_obs: int = 0
    product: ProductType = ProductType.INTRADAY
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def option_series(self) -> str:
        return series_of(self.option_key)


class TradeManager:
    def __init__(self, db: Database, bus: EventBus, clock: Clock, settings: Callable[[], GlobalSettings],
                 market: MarketDataService, orders: OrderManager, exits: ExitManager, risk: RiskManager,
                 accounts: Callable[[int], AccountRuntime | None]) -> None:
        self.db = db
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.market = market
        self.orders = orders
        self.exits = exits
        self.risk = risk
        self.accounts = accounts
        self.live: dict[int, LiveTrade] = {}
        self.realized_today: dict[int, tuple[int | None, int, float]] = {}  # trade id -> (instance, account, gross)
        self.trade_counts: dict[int, int] = {}  # instance id -> trades today
        self.recon: dict[tuple[int, bool], dict[str, float]] = {}  # (account, paper) -> token -> diff
        self._recon_logged: dict[tuple[int, bool, str], datetime] = {}
        self._day: date | None = None
        self.on_trade_event: Callable[[int, TradeInfo], None] | None = None
        exits.trades = self
        risk.trades = self

    # ------------------------------------------------------------------ helpers
    def _emit(self, category: Category, event_type: str, message: str, level: Level = Level.INFO,
              lt: LiveTrade | None = None, **payload: Any) -> None:
        kw: dict[str, Any] = {}
        if lt is not None:
            kw = dict(instance_id=lt.instance_id, instance_name=lt.instance_name, account_id=lt.account_id,
                      account_label=lt.account_label, trade_id=lt.ref)
        self.bus.emit(category, event_type, message, level, **kw, **payload)

    def _route(self, lt: LiveTrade) -> tuple[AccountRuntime, Any, BrokerSync]:
        acc = self.accounts(lt.account_id)
        if acc is None:
            raise RuntimeError(f"account {lt.account_id} not loaded")
        adapter, sync = acc.order_route(lt.mode)
        return acc, adapter, sync

    def _notify(self, lt: LiveTrade, status: str, reason: str | None = None, exit_price: float | None = None,
                pnl: float | None = None) -> None:
        if self.on_trade_event and lt.instance_id is not None:
            self.on_trade_event(lt.instance_id, TradeInfo(trade_id=lt.ref, slot_id=lt.slot_id, direction=lt.direction,
                                                          status=status, exit_reason=reason,
                                                          entry_price=lt.entry_price, exit_price=exit_price, pnl=pnl))

    def load_today(self) -> None:
        today = self.clock.now().date()
        self._day = today
        self.realized_today.clear()
        self.trade_counts.clear()
        with self.db.session() as s:
            for t in s.scalars(select(Trade).where(Trade.trade_date == today)):
                if t.instance_id is not None and t.status != "FAILED":
                    self.trade_counts[t.instance_id] = self.trade_counts.get(t.instance_id, 0) + 1
                if t.status == "CLOSED" and t.gross_pnl is not None:
                    self.realized_today[t.id] = (t.instance_id, t.account_id or 0, t.gross_pnl)

    def _roll_day(self) -> None:
        if self._day != self.clock.now().date():
            self.load_today()

    # ------------------------------------------------------------------ metrics used by risk + UI
    def open_trade_count(self, instance_id: int | None = None, account_id: int | None = None) -> int:
        return sum(1 for lt in self.live.values()
                   if (instance_id is None or lt.instance_id == instance_id)
                   and (account_id is None or lt.account_id == account_id))

    def trades_today(self, instance_id: int) -> int:
        self._roll_day()
        return self.trade_counts.get(instance_id, 0)

    def orders_today(self, account_id: int) -> int:
        start = combine(self.clock.now().date(), datetime.min.time())
        with self.db.session() as s:
            return s.scalar(select(func.count(OrderRecord.id)).where(
                OrderRecord.account_id == account_id, OrderRecord.is_external.is_(False),
                OrderRecord.placed_at >= start)) or 0

    def unrealized(self, lt: LiveTrade) -> float:
        ltp = self.market.ltp(lt.option_series)
        if ltp is None or not lt.open_qty:
            return 0.0
        return (ltp - lt.entry_price) * lt.open_qty

    def daily_pnl(self, day: date, instance_id: int | None = None, account_id: int | None = None) -> float:
        self._roll_day()
        total = 0.0
        for inst, acc, gross in self.realized_today.values():
            if (instance_id is None or inst == instance_id) and (account_id is None or acc == account_id):
                total += gross
        for lt in self.live.values():
            if (instance_id is None or lt.instance_id == instance_id) and (account_id is None or lt.account_id == account_id):
                total += self.unrealized(lt)
        return round(total, 2)

    # ------------------------------------------------------------------ entry
    async def open_trade(self, rt: "InstanceRuntime", signal: EntrySignal) -> TradeInfo | None:
        self._roll_day()
        acc = self.accounts(rt.account_id) if rt.account_id else None
        base_kw = dict(instance_id=rt.instance_id, instance_name=rt.name)
        if acc is None:
            self.bus.emit(Category.STRATEGY, "SIGNAL_REJECTED", "reason=no_broker_account", Level.ERROR, **base_kw)
            return None
        base_kw.update(account_id=acc.id, account_label=acc.label)
        try:
            if rt.mode == "paper":
                await acc.ensure_shadow_started()
            adapter, sync = acc.order_route(rt.mode)
            inst = adapter.resolve_symbol(signal.option)
        except Exception as exc:
            self.bus.emit(Category.STRATEGY, "SIGNAL_REJECTED", f"reason=instrument_not_found {signal.option.label()}: {exc}",
                          Level.ERROR, reason="instrument_not_found", **base_kw)
            return None
        qty = signal.lots * inst.lot_size
        try:
            await self.market.subscribe(signal.option)
        except Exception as exc:
            log.warning("option subscribe failed: %s", exc)
        premium = await self.market.fetch_ltp(signal.option)
        self.bus.emit(
            Category.STRATEGY, "SIGNAL",
            f"dir={signal.direction.value} spot={signal.spot:.2f} trigger={signal.trigger:.2f} "
            f"strike={signal.option.strike:g} expiry={signal.option.expiry} symbol={inst.tradingsymbol} "
            f"token={inst.token} lots={signal.lots} qty={qty} premium={premium if premium is not None else '-'}",
            direction=signal.direction.value, spot=signal.spot, trigger=signal.trigger, strike=signal.option.strike,
            expiry=signal.option.expiry, symbol=inst.tradingsymbol, token=inst.token, lot_size=inst.lot_size,
            lots=signal.lots, quantity=qty, premium=premium, slot=signal.slot_id, mode=rt.mode, **base_kw)
        decision = self.risk.check_entry(instance_id=rt.instance_id, limits=rt.risk_limits, account=acc, mode=rt.mode,
                                         lots=signal.lots, quantity=qty, premium=premium)
        if not decision.ok:
            self.bus.emit(Category.STRATEGY, "SIGNAL_REJECTED",
                          f"dir={signal.direction.value} reason={decision.reason} level={decision.level}",
                          Level.WARNING, reason=decision.reason, risk_level=decision.level, **base_kw)
            return None
        if signal.order_type is OrderType.LIMIT and not premium:
            self.bus.emit(Category.STRATEGY, "SIGNAL_REJECTED", "reason=no_premium_for_limit_order", Level.WARNING,
                          **base_kw)
            return None
        plan = signal.exit_plan
        sq = combine(self.clock.now().date(), self.settings().market.final_square_off)
        with self.db.session() as s:
            row = Trade(trade_ref=next_trade_ref(s), instance_id=rt.instance_id, account_id=acc.id, mode=rt.mode,
                        trade_date=self.clock.now().date(), slot_id=signal.slot_id, direction=signal.direction.value,
                        exchange=inst.exchange.value, tradingsymbol=inst.tradingsymbol, token=inst.token,
                        strike=inst.strike, expiry=inst.expiry, lots=signal.lots, lot_size=inst.lot_size, quantity=0,
                        open_quantity=0, signal_time=signal.signal_time or self.clock.now(), signal_spot=signal.spot,
                        signal_trigger=signal.trigger, signal_premium=premium, spot_sl=plan.spot_sl,
                        spot_sl_side=plan.spot_sl_side, tp_spot_level=plan.tp_spot_level,
                        tp_spot_side=plan.tp_spot_side, tp_premium_pct=plan.tp_premium_pct,
                        exit_at=min(plan.exit_at, sq), status="PENDING_ENTRY",
                        meta={**signal.meta, "protective_sl_pct": plan.protective_sl_pct,
                              "plan_exit_at": plan.exit_at.isoformat(), "tick_size": inst.tick_size,
                              "spot_key": plan.underlying and {"underlying": plan.underlying.underlying,
                                                               "exchange": plan.underlying.exchange.value,
                                                               "instrument_type": plan.underlying.instrument_type.value}})
            s.add(row)
            s.flush()
            trade_id, trade_ref = row.id, row.trade_ref
        self.trade_counts[rt.instance_id] = self.trade_counts.get(rt.instance_id, 0) + 1
        price = None
        if signal.order_type is OrderType.LIMIT and premium:
            price = round_to_tick(premium * (1 + signal.limit_slippage_pct / 100), inst.tick_size, "up")
        req = OrderRequest(instrument=inst, side=Side.BUY, quantity=qty, order_type=signal.order_type,
                           product=ProductType.INTRADAY, price=price,
                           tag=make_tag(rt.instance_id, trade_id, "ENTRY"))
        placed = await self.orders.place(adapter, sync, req, purpose="ENTRY", trade_db_id=trade_id, trade_ref=trade_ref,
                                         instance_id=rt.instance_id, instance_name=rt.name, signal_price=premium,
                                         lots=signal.lots)
        if placed.order_id is None:
            self._fail(trade_id, "entry_rejected", placed.error)
            self.trade_counts[rt.instance_id] -= 1
            return TradeInfo(trade_id=trade_ref, slot_id=signal.slot_id, direction=signal.direction.value,
                             status="FAILED", exit_reason="entry_rejected")
        timeout = self.settings().sync.fill_timeout_sec
        final = await self.orders.wait_terminal(sync, placed.order_id, timeout)
        if final is None or not final.status.is_terminal:
            final = await self.orders.cancel(adapter, sync, placed.order_id, trade_ref=trade_ref,
                                             instance_id=rt.instance_id, reason="entry not filled in time") or final
        filled = final.filled_quantity if final else 0
        if filled <= 0:
            msg = final.status_message if final else "no fill"
            reason = "entry_rejected" if final and final.status is OrderStatus.REJECTED else "entry_not_filled"
            self._fail(trade_id, reason, msg)
            self.trade_counts[rt.instance_id] -= 1
            return TradeInfo(trade_id=trade_ref, slot_id=signal.slot_id, direction=signal.direction.value,
                             status="FAILED", exit_reason=reason)
        assert final is not None
        if filled < qty:
            self.bus.emit(Category.ORDER, "PARTIAL_ENTRY", f"filled {filled}/{qty}; trading the filled quantity",
                          Level.WARNING, trade_id=trade_ref, **base_kw)
        entry_price = self._avg_fill(sync, [placed.order_id]) or final.average_price
        lt = LiveTrade(
            id=trade_id, ref=trade_ref, instance_id=rt.instance_id, instance_name=rt.name, account_id=acc.id,
            account_label=acc.label, mode=rt.mode, direction=signal.direction.value, slot_id=signal.slot_id,
            symbol=inst.tradingsymbol, token=inst.token, exchange=inst.exchange, option_key=signal.option,
            spot_key=InstrumentKey(underlying=plan.underlying.underlying, exchange=plan.underlying.exchange,
                                   instrument_type=plan.underlying.instrument_type) if plan.underlying else None,
            lots=signal.lots, lot_size=inst.lot_size, tick_size=inst.tick_size, open_qty=filled, entry_qty=filled,
            entry_price=entry_price, entry_time=final.exchange_time or self.clock.now(),
            exit_at=min(plan.exit_at, sq), spot_sl=plan.spot_sl, spot_sl_side=plan.spot_sl_side,
            tp_spot_level=plan.tp_spot_level, tp_spot_side=plan.tp_spot_side,
            tp_premium_level=round(entry_price * (1 + plan.tp_premium_pct / 100), 2) if plan.tp_premium_pct else None,
            protective_pct=plan.protective_sl_pct)
        self.live[trade_id] = lt
        self._save(lt, status="OPEN", quantity=filled, open_quantity=filled, entry_price=entry_price,
                   entry_time=lt.entry_time, entry_order_id=placed.order_id, tp_premium_level=lt.tp_premium_level)
        await self._place_protective(lt, adapter, sync)
        self._register_exit(lt)
        self._emit(Category.POSITION, "OPENED",
                   f"symbol={lt.symbol} qty={filled} avg={entry_price:.2f} spot_SL={_f(lt.spot_sl)} "
                   f"protective_sl={lt.protective_sl_order_id or '-'} TP={_f(lt.tp_spot_level or lt.tp_premium_level)} "
                   f"exit={fmt_hm(lt.exit_at)}", lt=lt, symbol=lt.symbol, quantity=filled, average_price=entry_price,
                   spot_sl=lt.spot_sl, protective_sl_order_id=lt.protective_sl_order_id, tp=lt.tp_spot_level,
                   tp_premium=lt.tp_premium_level, exit_at=lt.exit_at, mode=lt.mode)
        self._emit(Category.STRATEGY, "ENTRY",
                   f"ENTRY {lt.direction} symbol={lt.symbol} strike={signal.option.strike:g} qty={filled} "
                   f"spot={signal.spot:.2f} spot_SL={_f(lt.spot_sl)} exit={fmt_hm(lt.exit_at)}", lt=lt,
                   premium=entry_price, lots=signal.lots)
        return TradeInfo(trade_id=trade_ref, slot_id=signal.slot_id, direction=signal.direction.value, status="OPEN",
                         entry_price=entry_price)

    def _fail(self, trade_id: int, reason: str, message: str | None) -> None:
        ref = inst = acc = None
        with self.db.session() as s:
            t = s.get(Trade, trade_id)
            if t is not None:
                t.status, t.exit_reason = "FAILED", reason
                t.meta = {**(t.meta or {}), "failure": message}
                ref, inst, acc = t.trade_ref, t.instance_id, t.account_id
        self.bus.emit(Category.POSITION, "ENTRY_FAILED", f"reason={reason} {message or ''}".strip(), Level.ERROR,
                      trade_id=ref, instance_id=inst, account_id=acc)

    def _save(self, lt: LiveTrade, **fields: Any) -> None:
        with self.db.session() as s:
            t = s.get(Trade, lt.id)
            if t is None:
                return
            for k, v in fields.items():
                setattr(t, k, v)

    def _avg_fill(self, sync: BrokerSync, order_ids: list[str]) -> float | None:
        qty = val = 0.0
        for oid in order_ids:
            fills = sync.fills_for(oid)
            if fills:
                for f in fills:
                    qty += f.quantity
                    val += f.quantity * f.price
            else:
                o = sync.orders.get(oid)
                if o and o.filled_quantity:
                    qty += o.filled_quantity
                    val += o.filled_quantity * o.average_price
        return round(val / qty, 4) if qty else None

    async def _ensure_subscribed(self, lt: LiveTrade) -> None:
        for key in (lt.option_key, lt.spot_key):
            if key is None:
                continue
            try:
                await self.market.subscribe(key)
            except Exception as exc:
                log.warning("subscribe %s failed: %s", key.label(), exc)
        if self.market.ltp(lt.option_series) is None:
            await self.market.fetch_ltp(lt.option_key)

    def _register_exit(self, lt: LiveTrade) -> None:
        try:
            asyncio.get_running_loop().create_task(self._ensure_subscribed(lt))
        except RuntimeError:
            pass
        sq = combine(self.clock.now().date(), self.settings().market.final_square_off)
        self.exits.register(ExitState(
            trade_db_id=lt.id, trade_ref=lt.ref, instance_id=lt.instance_id, instance_name=lt.instance_name,
            account_id=lt.account_id, mode=lt.mode, direction=lt.direction,
            spot_series=series_of(lt.spot_key) if lt.spot_key else None, option_series=lt.option_series,
            option_key=lt.option_key, spot_key=lt.spot_key, entry_price=lt.entry_price, quantity=lt.open_qty,
            exit_at=lt.exit_at, square_off_at=sq, spot_sl=lt.spot_sl, spot_sl_side=lt.spot_sl_side,
            tp_spot_level=lt.tp_spot_level, tp_spot_side=lt.tp_spot_side, tp_premium_level=lt.tp_premium_level,
            last_spot=self.market.ltp(lt.spot_key) if lt.spot_key else None,
            last_premium=self.market.ltp(lt.option_series)))


    async def _place_protective(self, lt: LiveTrade, adapter: Any, sync: BrokerSync) -> None:
        if not lt.protective_pct:
            return
        trigger = round_to_tick(lt.entry_price * (1 - lt.protective_pct / 100), lt.tick_size, "down")
        if trigger < lt.tick_size:
            return
        inst = adapter.find_instrument(lt.exchange, lt.token) or adapter.resolve_symbol(lt.option_key)
        for order_type in (OrderType.SL_M, OrderType.SL):
            limit = round_to_tick(trigger * 0.95, lt.tick_size, "down") if order_type is OrderType.SL else None
            req = OrderRequest(instrument=inst, side=Side.SELL, quantity=lt.open_qty, order_type=order_type,
                               product=lt.product, trigger_price=trigger, price=limit,
                               tag=make_tag(lt.instance_id or 0, lt.id, "PROTECTIVE_SL"))
            placed = await self.orders.place(adapter, sync, req, purpose="PROTECTIVE_SL", trade_db_id=lt.id,
                                             trade_ref=lt.ref, instance_id=lt.instance_id,
                                             instance_name=lt.instance_name, signal_price=trigger, lots=lt.lots,
                                             attempts=2)
            if placed.order_id is None:
                continue
            o = await self.orders.wait_working_or_terminal(sync, placed.order_id, 5.0)
            if o is not None and o.status is OrderStatus.REJECTED:
                if order_type is OrderType.SL_M:
                    self._emit(Category.ORDER, "SL_M_REJECTED", f"SL-M rejected ({o.status_message}); trying SL-limit",
                               Level.WARNING, lt=lt)
                    continue
                break
            lt.protective_sl_order_id, lt.protective_sl_trigger = placed.order_id, trigger
            lt.protective_sl_status = o.status.value if o else "OPEN"
            self._save(lt, protective_sl_order_id=placed.order_id, protective_sl_trigger=trigger,
                       protective_sl_status=lt.protective_sl_status)
            self._emit(Category.ORDER, "SL_PLACED",
                       f"order_id={placed.order_id} type={order_type.value.replace('_', '-')} trigger={trigger:.2f} "
                       f"status={lt.protective_sl_status}", lt=lt, order_id=placed.order_id, trigger=trigger,
                       order_type=order_type.value, status=lt.protective_sl_status)
            return
        self._emit(Category.ORDER, "PROTECTIVE_SL_FAILED",
                   "could not place the protective stop at the broker; spot SL is still managed by the platform",
                   Level.CRITICAL, lt=lt)

    # ------------------------------------------------------------------ exit
    async def exit_trade(self, trade_db_id: int, reason: str, *, spot: float | None = None,
                         premium: float | None = None, by: str | None = None) -> bool:
        lt = self.live.get(trade_db_id)
        if lt is None:
            return False
        async with lt.lock:
            if lt.status != "OPEN" or trade_db_id not in self.live:
                return False
            lt.status = "EXITING"
            self._save(lt, status="EXITING", exit_trigger_spot=spot, exit_trigger_premium=premium)
            self._emit(Category.EXIT, "TRIGGERED",
                       f"reason={reason} spot={_f(spot)} premium={_f(premium)}" + (f" by={by}" if by else ""),
                       Level.INFO, lt=lt, reason=reason, spot=spot, premium=premium, by=by)
            try:
                acc, adapter, sync = self._route(lt)
            except Exception as exc:
                self._emit(Category.ERROR, "EXIT_FAILED", f"no route to broker: {exc}", Level.CRITICAL, lt=lt)
                lt.status = "OPEN"
                return False
            # 1. protective SL: already filled at the broker? otherwise cancel it first (avoid double sell)
            if lt.protective_sl_order_id:
                sl = sync.orders.get(lt.protective_sl_order_id)
                if sl is None or not sl.status.is_terminal:
                    lt.cancelling_sl = True
                    sl = await self.orders.cancel(adapter, sync, lt.protective_sl_order_id, trade_ref=lt.ref,
                                                  instance_id=lt.instance_id, reason=f"exit:{reason}")
                    lt.cancelling_sl = False
                if sl is not None and sl.filled_quantity > 0:
                    lt.exit_order_ids.append(sl.order_id)
                    lt.open_qty = max(0, lt.open_qty - sl.filled_quantity)
                    if lt.open_qty == 0:
                        await self._finalize(lt, "protective_sl" if reason not in ("kill_switch",) else reason, sync)
                        return True
            # 2. never sell more than the broker says we hold
            qty = lt.open_qty
            pos = sync.position_for(lt.token)
            fresh = sync.positions_at is not None and (self.clock.now() - sync.positions_at).total_seconds() < 15
            if pos is not None and fresh and 0 <= pos.net_quantity < qty and not lt.mode == "paper":
                qty = pos.net_quantity
            if qty <= 0:
                await self._finalize(lt, "manual_exit_at_broker", sync)
                return True
            inst = adapter.find_instrument(lt.exchange, lt.token) or adapter.resolve_symbol(lt.option_key)
            age = self.market.ltp_age(lt.option_key)
            if age is None or age > 5:
                await self.market.fetch_ltp(lt.option_key)  # paper fills and the log need a current premium
            remaining = qty
            for attempt in range(1, 4):
                req = OrderRequest(instrument=inst, side=Side.SELL, quantity=remaining, order_type=OrderType.MARKET,
                                   product=lt.product, tag=make_tag(lt.instance_id or 0, lt.id,
                                                                    "KILL" if reason == "kill_switch" else "EXIT"))
                placed = await self.orders.place(adapter, sync, req, purpose="EXIT", trade_db_id=lt.id,
                                                 trade_ref=lt.ref, instance_id=lt.instance_id,
                                                 instance_name=lt.instance_name, signal_price=premium, lots=lt.lots)
                if placed.order_id is None:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                lt.exit_order_ids.append(placed.order_id)
                self._save(lt, exit_order_id=placed.order_id)
                final = await self.orders.wait_terminal(sync, placed.order_id, self.settings().sync.fill_timeout_sec)
                filled = final.filled_quantity if final else 0
                remaining -= filled
                lt.open_qty = max(0, lt.open_qty - filled)
                if remaining <= 0:
                    break
                if final is not None and not final.status.is_terminal:
                    await self.orders.cancel(adapter, sync, placed.order_id, trade_ref=lt.ref, reason="exit retry")
            if remaining > 0:
                lt.status = "OPEN"
                self._save(lt, status="OPEN", open_quantity=lt.open_qty)
                self._emit(Category.ERROR, "EXIT_FAILED",
                           f"could not exit {remaining} units of {lt.symbol}; retrying in 5s — CHECK THE BROKER",
                           Level.CRITICAL, lt=lt, remaining=remaining, reason=reason)
                st = self.exits.states.get(lt.id)
                loop = asyncio.get_running_loop()
                loop.call_later(5, lambda: loop.create_task(self.exit_trade(lt.id, reason, spot=spot, premium=premium)))
                if st is not None:
                    st.quantity = lt.open_qty
                return False
            await self._finalize(lt, reason, sync)
            return True

    async def _finalize(self, lt: LiveTrade, reason: str, sync: BrokerSync, exit_price: float | None = None) -> None:
        self.exits.unregister(lt.id)
        self.live.pop(lt.id, None)
        try:
            acc, adapter, _ = self._route(lt)
            # cancel anything still working for this trade (e.g. a leftover protective SL)
            for o in list(sync.orders.values()):
                parsed = parse_tag(o.tag)
                if o.status.is_working and parsed is not None and parsed[1] == lt.id:
                    lt.cancelling_sl = True
                    await self.orders.cancel(adapter, sync, o.order_id, trade_ref=lt.ref, reason="trade closed")
                    lt.cancelling_sl = False
        except Exception as exc:
            log.warning("cleanup after close failed: %s", exc)
        entry = self._avg_fill(sync, [o for o in self._entry_orders(lt)]) or lt.entry_price
        out = exit_price or self._avg_fill(sync, lt.exit_order_ids) or self._external_exit_price(lt, sync)
        qty = lt.entry_qty
        gross = round((out - entry) * qty, 2) if out else None
        rates = self.settings().charges
        ch = option_charges(rates, entry * qty, (out or 0) * qty).total if out else None
        net = round(gross - ch, 2) if gross is not None and ch is not None else None
        now = self.clock.now()
        self._save(lt, status="CLOSED", open_quantity=0, exit_reason=reason, exit_price=out, exit_time=now,
                   gross_pnl=gross, charges=ch, net_pnl=net, entry_price=entry,
                   protective_sl_status=(sync.orders.get(lt.protective_sl_order_id).status.value
                                         if lt.protective_sl_order_id and lt.protective_sl_order_id in sync.orders
                                         else lt.protective_sl_status))
        self.realized_today[lt.id] = (lt.instance_id, lt.account_id, gross or 0.0)
        dur = format_duration(now - lt.entry_time) if lt.entry_time else "-"
        self._emit(Category.STRATEGY, "EXIT",
                   f"EXIT reason={reason} premium_in={entry:.2f} premium_out={_f(out)} pnl={_f(gross)}", lt=lt,
                   reason=reason, premium_in=entry, premium_out=out, pnl=gross)
        self._emit(Category.POSITION, "CLOSED",
                   f"entry={entry:.2f} exit={_f(out)} qty={qty} gross={_f(gross)} charges={_f(ch)} net={_f(net)} "
                   f"duration={dur}", lt=lt, entry=entry, exit=out, quantity=qty, gross=gross, charges=ch, net=net,
                   duration=dur, reason=reason)
        self._notify(lt, "CLOSED", reason, out, gross)

    def _entry_orders(self, lt: LiveTrade) -> list[str]:
        with self.db.session() as s:
            return [r for r in s.scalars(select(OrderRecord.broker_order_id).where(
                OrderRecord.trade_id == lt.id, OrderRecord.purpose == "ENTRY"))]

    def _external_exit_price(self, lt: LiveTrade, sync: BrokerSync) -> float | None:
        """Exit price for a manual exit at the broker: sells of this token after our entry."""
        since = lt.entry_time or self.clock.now() - timedelta(hours=8)
        sells = [f for f in sync.fills.values() if f.token == lt.token and f.side is Side.SELL and f.time >= since
                 and f.order_id not in self._entry_orders(lt)]
        if sells:
            q = sum(f.quantity for f in sells)
            return round(sum(f.price * f.quantity for f in sells) / q, 4) if q else None
        pos = sync.position_for(lt.token)
        if pos is not None and pos.sell_average:
            return pos.sell_average
        return self.market.ltp(lt.option_series)

    # ------------------------------------------------------------------ broker-driven events
    def on_order(self, sync: BrokerSync, o: Order, prev: Order | None, rec: OrderRecord) -> None:
        lt = self.live.get(rec.trade_id) if rec.trade_id else None
        loop = asyncio.get_running_loop()
        if lt is not None and rec.purpose == "PROTECTIVE_SL" and o.order_id == lt.protective_sl_order_id:
            lt.protective_sl_status = o.status.value
            if o.trigger_price and o.trigger_price != lt.protective_sl_trigger:
                lt.protective_sl_trigger = o.trigger_price
            self._save(lt, protective_sl_status=o.status.value, protective_sl_trigger=lt.protective_sl_trigger)
            if o.status is OrderStatus.COMPLETE and lt.status == "OPEN":
                loop.create_task(self._protective_filled(lt.id, o))
            elif o.status is OrderStatus.CANCELLED and not lt.cancelling_sl and lt.status == "OPEN":
                self._emit(Category.RISK, "PROTECTIVE_SL_CANCELLED_EXTERNALLY",
                           f"protective SL {o.order_id} on {lt.symbol} was cancelled at the broker — the position "
                           f"now has no broker-side stop", Level.CRITICAL, lt=lt, order_id=o.order_id)
        elif rec.is_external and o.status is OrderStatus.COMPLETE and o.side is Side.SELL:
            if any(t.token == o.token and t.account_id == sync.account_id and (t.mode == "paper") == sync.paper
                   for t in self.live.values()):
                loop.create_task(sync.poll_once(full=True))  # positions listener confirms the manual exit

    async def _protective_filled(self, trade_db_id: int, o: Order) -> None:
        lt = self.live.get(trade_db_id)
        if lt is None:
            return
        async with lt.lock:
            if lt.status != "OPEN":
                return
            lt.status = "EXITING"
            _, _, sync = self._route(lt)
            lt.exit_order_ids.append(o.order_id)
            lt.open_qty = max(0, lt.open_qty - o.filled_quantity)
            self._emit(Category.EXIT, "TRIGGERED", f"reason=protective_sl premium={o.average_price:.2f}",
                       Level.WARNING, lt=lt, reason="protective_sl")
            await self._finalize(lt, "protective_sl", sync)

    def on_positions(self, sync: BrokerSync, positions: list[Position]) -> None:
        mine = [t for t in self.live.values() if t.account_id == sync.account_id and (t.mode == "paper") == sync.paper]
        if mine:
            by_token: dict[str, list[LiveTrade]] = {}
            for t in mine:
                by_token.setdefault(t.token, []).append(t)
            now = self.clock.now()
            for token, trades in by_token.items():
                expected = sum(t.open_qty for t in trades if t.status == "OPEN")
                if expected <= 0:
                    continue
                pos = sync.position_for(token)
                actual = pos.net_quantity if pos else 0
                if actual >= expected:
                    for t in trades:
                        t.missing_obs = 0
                    continue
                # Only trust a shortfall after it has been seen twice and the trade has settled
                trades.sort(key=lambda t: t.entry_time or now, reverse=True)
                for t in trades:
                    if t.status != "OPEN" or (t.entry_time and (now - t.entry_time).total_seconds() < 5):
                        continue
                    t.missing_obs += 1
                    if t.missing_obs >= 2:
                        asyncio.get_running_loop().create_task(self._manual_exit(t.id, max(0, actual)))
                    break
        self._reconcile_pnl(sync, positions)

    async def _manual_exit(self, trade_db_id: int, broker_qty: int) -> None:
        lt = self.live.get(trade_db_id)
        if lt is None:
            return
        async with lt.lock:
            if lt.status != "OPEN":
                return
            acc, adapter, sync = self._route(lt)
            if broker_qty > 0 and broker_qty < lt.open_qty:
                lt.open_qty = broker_qty
                lt.missing_obs = 0
                self._save(lt, open_quantity=broker_qty)
                self._emit(Category.POSITION, "PARTIAL_MANUAL_EXIT",
                           f"position reduced at the broker to {broker_qty}; adjusting protective SL", Level.WARNING,
                           lt=lt)
                st = self.exits.states.get(lt.id)
                if st:
                    st.quantity = broker_qty
                if lt.protective_sl_order_id:
                    from brokers.base import OrderModify

                    inst = adapter.find_instrument(lt.exchange, lt.token)
                    if inst is not None:
                        sl = sync.orders.get(lt.protective_sl_order_id)
                        await self.orders.modify(adapter, sync, OrderModify(
                            order_id=lt.protective_sl_order_id, instrument=inst,
                            order_type=sl.order_type if sl else OrderType.SL_M, quantity=broker_qty,
                            price=sl.price if sl else None, trigger_price=lt.protective_sl_trigger), trade_ref=lt.ref)
                return
            lt.status = "EXITING"
            self._emit(Category.EXIT, "TRIGGERED", "reason=manual_exit_at_broker (position closed outside the platform)",
                       Level.WARNING, lt=lt, reason="manual_exit_at_broker")
            await self._finalize(lt, "manual_exit_at_broker", sync)

    def _reconcile_pnl(self, sync: BrokerSync, positions: list[Position]) -> None:
        """Compare platform P&L with the broker's per-symbol P&L where all activity is ours."""
        today = self.clock.now().date()
        key = (sync.account_id, sync.paper)
        diffs: dict[str, float] = {}
        with self.db.session() as s:
            ext_tokens = set(s.scalars(select(OrderRecord.token).where(
                OrderRecord.account_id == sync.account_id, OrderRecord.paper == sync.paper,
                OrderRecord.is_external.is_(True), OrderRecord.placed_at >= combine(today, datetime.min.time()))))
            closed = list(s.scalars(select(Trade).where(Trade.account_id == sync.account_id, Trade.trade_date == today,
                                                        Trade.mode == ("paper" if sync.paper else "live"))))
        tol = self.settings().sync.pnl_tolerance
        for pos in positions:
            if pos.token in ext_tokens:
                continue
            platform = 0.0
            involved = False
            for t in closed:
                if t.token != pos.token:
                    continue
                involved = True
                if t.status == "CLOSED" and t.gross_pnl is not None:
                    platform += t.gross_pnl
            for lt in self.live.values():
                if lt.token == pos.token and lt.account_id == sync.account_id and (lt.mode == "paper") == sync.paper:
                    involved = True
                    ltp = pos.ltp or self.market.ltp(lt.option_series) or lt.entry_price
                    platform += (ltp - lt.entry_price) * lt.open_qty
            if not involved:
                continue
            diff = round(platform - pos.pnl, 2)
            diffs[pos.token] = diff
            if abs(diff) > tol:
                lk = (sync.account_id, sync.paper, pos.token)
                last = self._recon_logged.get(lk)
                if last is None or (self.clock.now() - last).total_seconds() > 60:
                    self._recon_logged[lk] = self.clock.now()
                    self.bus.emit(Category.RISK, "PNL_MISMATCH",
                                  f"{pos.tradingsymbol}: platform {platform:.2f} vs broker {pos.pnl:.2f} (diff {diff:+.2f})",
                                  Level.WARNING, account_id=sync.account_id, account_label=sync.account_label,
                                  platform=round(platform, 2), broker=pos.pnl, diff=diff, symbol=pos.tradingsymbol)
        self.recon[key] = diffs

    def mtm_snapshot(self, st: ExitState) -> None:
        lt = self.live.get(st.trade_db_id)
        if lt is None:
            return
        ltp = self.market.ltp(lt.option_series)
        unreal = round((ltp - lt.entry_price) * lt.open_qty, 2) if ltp is not None else None
        broker_pnl = None
        try:
            _, _, sync = self._route(lt)
            pos = sync.position_for(lt.token)
            if pos is not None:
                broker_pnl = pos.pnl
        except Exception:
            pass
        diff = self.recon.get((lt.account_id, lt.mode == "paper"), {}).get(lt.token)
        self._emit(Category.POSITION, "MTM",
                   f"ltp={_f(ltp)} unrealized={_f(unreal)} broker_pnl={_f(broker_pnl)} diff={_f(diff)}",
                   Level.DEBUG, lt=lt, ltp=ltp, unrealized=unreal, broker_pnl=broker_pnl, diff=diff)

    # ------------------------------------------------------------------ restart reconciliation
    async def reconcile_account(self, acc: AccountRuntime) -> None:
        today = self.clock.now().date()
        with self.db.session() as s:
            rows = list(s.scalars(select(Trade).where(Trade.account_id == acc.id, Trade.status.in_(ACTIVE))))
            for r in rows:
                s.expunge(r)
        if not rows:
            return
        if any(r.mode == "paper" for r in rows):
            await acc.ensure_shadow_started()
        for sync in acc.all_syncs():
            await sync.poll_once(full=True)
        for row in rows:
            try:
                await self._reconcile_trade(acc, row, today)
            except Exception as exc:
                self.bus.exception("RECONCILE_FAILED", f"trade {row.trade_ref}", exc, account_id=acc.id,
                                   trade_id=row.trade_ref)

    async def _reconcile_trade(self, acc: AccountRuntime, row: Trade, today: date) -> None:
        adapter, sync = acc.order_route(row.mode)
        kw = dict(trade_id=row.trade_ref, instance_id=row.instance_id, account_id=acc.id, account_label=acc.label)
        if row.trade_date < today:
            with self.db.session() as s:
                t = s.get(Trade, row.id)
                t.status, t.exit_reason = "CLOSED", "stale_previous_session"
            self.bus.emit(Category.POSITION, "CLOSED", "trade from a previous session closed (intraday auto square-off)",
                          Level.WARNING, **kw)
            return
        lt = self._live_from_row(acc, row)
        entry = sync.orders.get(row.entry_order_id) if row.entry_order_id else None
        if row.status == "PENDING_ENTRY":
            if entry is not None and entry.status.is_working:
                entry = await self.orders.cancel(adapter, sync, entry.order_id, trade_ref=row.trade_ref,
                                                 reason="restart during entry") or entry
            if entry is None or entry.filled_quantity <= 0:
                self._fail(row.id, "entry_interrupted", "app restarted before the entry filled")
                return
            lt.entry_price = entry.average_price
            lt.open_qty = lt.entry_qty = entry.filled_quantity
            self._save(lt, status="OPEN", quantity=lt.entry_qty, open_quantity=lt.open_qty, entry_price=lt.entry_price)
        pos = sync.position_for(row.token)
        held = pos.net_quantity if pos else 0
        sl = sync.orders.get(row.protective_sl_order_id) if row.protective_sl_order_id else None
        if row.status == "EXITING":
            done = [sync.orders[o] for o in [row.exit_order_id] if o and o in sync.orders]
            if held <= 0 or (done and done[0].status is OrderStatus.COMPLETE):
                lt.exit_order_ids = [o.order_id for o in done]
                await self._finalize(lt, row.exit_reason or "timed_exit", sync)
                return
        if held <= 0:
            if sl is not None and sl.status is OrderStatus.COMPLETE:
                lt.exit_order_ids = [sl.order_id]
                await self._finalize(lt, "protective_sl", sync)
            else:
                await self._finalize(lt, "manual_exit_at_broker", sync)
            return
        lt.open_qty = min(lt.open_qty or held, held)
        self.live[lt.id] = lt
        self._register_exit(lt)
        if lt.protective_pct and (sl is None or sl.status.is_terminal):
            await self._place_protective(lt, adapter, sync)
        self.bus.emit(Category.POSITION, "RESUMED",
                      f"resumed {lt.symbol} qty={lt.open_qty} avg={lt.entry_price:.2f} exit={fmt_hm(lt.exit_at)}",
                      Level.WARNING, **kw)
        if row.status == "EXITING":
            await self.exit_trade(lt.id, row.exit_reason or "timed_exit")

    def _live_from_row(self, acc: AccountRuntime, row: Trade) -> LiveTrade:
        meta = row.meta or {}
        sk = meta.get("spot_key")
        spot_key = InstrumentKey(underlying=sk["underlying"], exchange=Exchange(sk["exchange"]),
                                 instrument_type=InstrumentType(sk["instrument_type"])) if sk else None
        option_key = InstrumentKey(underlying=(spot_key.underlying if spot_key else str(meta.get("underlying") or "NIFTY")),
                                   exchange=Exchange(row.exchange),
                                   instrument_type=InstrumentType.CE if row.direction == "CE_BUY" else InstrumentType.PE,
                                   expiry=row.expiry, strike=row.strike)
        with self.db.session() as s:
            from storage.models import StrategyInstance

            inst = s.get(StrategyInstance, row.instance_id) if row.instance_id else None
            name = inst.name if inst else "-"
            exit_ids = [r for r in s.scalars(select(OrderRecord.broker_order_id).where(
                OrderRecord.trade_id == row.id, OrderRecord.purpose == "EXIT"))]
        return LiveTrade(
            id=row.id, ref=row.trade_ref, instance_id=row.instance_id, instance_name=name, account_id=acc.id,
            account_label=acc.label, mode=row.mode, direction=row.direction, slot_id=row.slot_id,
            symbol=row.tradingsymbol, token=row.token, exchange=Exchange(row.exchange), option_key=option_key,
            spot_key=spot_key, lots=row.lots, lot_size=row.lot_size, tick_size=float(meta.get("tick_size", 0.05)),
            open_qty=row.open_quantity, entry_qty=row.quantity, entry_price=row.entry_price or 0.0,
            entry_time=row.entry_time, exit_at=row.exit_at or self.clock.now(), spot_sl=row.spot_sl,
            spot_sl_side=row.spot_sl_side, tp_spot_level=row.tp_spot_level, tp_spot_side=row.tp_spot_side,
            tp_premium_level=row.tp_premium_level, protective_pct=meta.get("protective_sl_pct"),
            protective_sl_order_id=row.protective_sl_order_id, protective_sl_trigger=row.protective_sl_trigger,
            protective_sl_status=row.protective_sl_status, status="OPEN", exit_order_ids=exit_ids)

    def today_trade_infos(self, instance_id: int) -> list[TradeInfo]:
        today = self.clock.now().date()
        with self.db.session() as s:
            rows = list(s.scalars(select(Trade).where(Trade.instance_id == instance_id, Trade.trade_date == today)))
        out = []
        for r in rows:
            status = "OPEN" if r.status in ACTIVE else r.status
            out.append(TradeInfo(trade_id=r.trade_ref, slot_id=r.slot_id, direction=r.direction, status=status,
                                 exit_reason=r.exit_reason, entry_price=r.entry_price, exit_price=r.exit_price,
                                 pnl=r.gross_pnl))
        return out

    # ------------------------------------------------------------------ kill switches
    async def exit_all(self, reason: str, *, account_id: int | None = None, instance_id: int | None = None,
                       by: str | None = None) -> int:
        targets = [lt for lt in list(self.live.values())
                   if (account_id is None or lt.account_id == account_id)
                   and (instance_id is None or lt.instance_id == instance_id)]
        results = await asyncio.gather(*(self.exit_trade(lt.id, reason, spot=self.market.ltp(lt.spot_key) if lt.spot_key else None,
                                                         premium=self.market.ltp(lt.option_series), by=by)
                                         for lt in targets), return_exceptions=True)
        return sum(1 for r in results if r is True)

    async def cancel_all_orders(self, acc: AccountRuntime, by: str | None = None) -> int:
        n = 0
        for sync in acc.all_syncs():
            adapter = sync.adapter
            for o in list(sync.orders.values()):
                if o.status.is_working:
                    for lt in self.live.values():
                        if lt.protective_sl_order_id == o.order_id:
                            lt.cancelling_sl = True
                    await self.orders.cancel(adapter, sync, o.order_id, reason=f"kill switch by {by or 'system'}")
                    n += 1
        return n

    async def flatten_account(self, acc: AccountRuntime, by: str | None = None) -> int:
        """Exit every remaining non-zero position in the account (including external ones)."""
        n = 0
        for sync in acc.all_syncs():
            await sync.poll_once(full=True)
            for pos in sync.positions:
                if pos.net_quantity == 0:
                    continue
                inst = sync.adapter.find_instrument(pos.exchange, pos.token)
                if inst is None:
                    self.bus.emit(Category.RISK, "KILL_SKIPPED", f"cannot resolve {pos.tradingsymbol}", Level.ERROR,
                                  account_id=acc.id)
                    continue
                side = Side.SELL if pos.net_quantity > 0 else Side.BUY
                req = OrderRequest(instrument=inst, side=side, quantity=abs(pos.net_quantity),
                                   order_type=OrderType.MARKET, product=pos.product, tag="I0T0K")
                placed = await self.orders.place(sync.adapter, sync, req, purpose="KILL", trade_db_id=None,
                                                 trade_ref=None, instance_id=None, instance_name=None)
                if placed.order_id:
                    n += 1
        return n

    # ------------------------------------------------------------------ UI
    def live_view(self) -> list[dict[str, Any]]:
        now = self.clock.now()
        out = []
        for lt in self.live.values():
            spot = self.market.ltp(lt.spot_key) if lt.spot_key else None
            ltp = self.market.ltp(lt.option_series)
            unreal = round((ltp - lt.entry_price) * lt.open_qty, 2) if ltp is not None else None
            sl_dist = None
            if spot is not None and lt.spot_sl is not None:
                sl_dist = round(spot - lt.spot_sl if lt.spot_sl_side == "below" else lt.spot_sl - spot, 2)
            tp_dist = None
            if lt.tp_spot_level is not None and spot is not None:
                tp_dist = round(lt.tp_spot_level - spot if lt.tp_spot_side == "above" else spot - lt.tp_spot_level, 2)
            elif lt.tp_premium_level is not None and ltp is not None:
                tp_dist = round(lt.tp_premium_level - ltp, 2)
            broker_pnl = None
            try:
                _, _, sync = self._route(lt)
                pos = sync.position_for(lt.token)
                broker_pnl = pos.pnl if pos else None
            except Exception:
                pass
            diff = self.recon.get((lt.account_id, lt.mode == "paper"), {}).get(lt.token)
            charges = option_charges(self.settings().charges, lt.entry_price * lt.open_qty,
                                     (ltp or lt.entry_price) * lt.open_qty).total
            out.append({
                "id": lt.id, "trade_ref": lt.ref, "instance_id": lt.instance_id, "instance_name": lt.instance_name,
                "account_id": lt.account_id, "account_label": lt.account_label, "mode": lt.mode,
                "direction": lt.direction, "symbol": lt.symbol, "qty": lt.open_qty, "lots": lt.lots,
                "entry_price": lt.entry_price, "ltp": ltp, "unrealized": unreal,
                "net_estimate": round(unreal - charges, 2) if unreal is not None else None,
                "spot": spot, "spot_sl": lt.spot_sl, "spot_sl_side": lt.spot_sl_side, "sl_distance": sl_dist,
                "protective_sl_order_id": lt.protective_sl_order_id, "protective_sl_trigger": lt.protective_sl_trigger,
                "protective_sl_status": lt.protective_sl_status,
                "tp_level": lt.tp_spot_level if lt.tp_spot_level is not None else lt.tp_premium_level,
                "tp_kind": "spot" if lt.tp_spot_level is not None else ("premium" if lt.tp_premium_level else None),
                "tp_distance": tp_dist, "exit_at": lt.exit_at.isoformat(),
                "seconds_to_exit": max(0, int((lt.exit_at - now).total_seconds())), "status": lt.status,
                "broker_pnl": broker_pnl, "pnl_diff": diff,
                "mismatch": diff is not None and abs(diff) > self.settings().sync.pnl_tolerance,
            })
        return out


def _f(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"
