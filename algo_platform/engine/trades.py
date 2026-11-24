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
from core.clock import Clock, combine, fmt_hm, format_duration, hm_or_hms
from core.events import Category, EventBus, Level
from core.settings_schema import GlobalSettings
from core.spot_sl import sl_view
from engine.accounts import AccountRuntime, BrokerSync, make_tag, parse_tag
from engine.charges import option_charges
from engine.exit_manager import (ExitManager, ExitState, RunnerState, band_pct, runner_from_plan,
                                 runner_on_fill)
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
    exit_rejects: int = 0  # exit sells refused by the broker for funds / margin in a row (retries slow down)
    blocked_by: str | None = None  # manual order(s) holding the quantity, already reported
    product: ProductType = ProductType.INTRADAY
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # scale-out targets (TP1, TP2): dicts {label, level, side, qty, done, filled, price}
    partials: list[dict[str, Any]] = field(default_factory=list)
    partial_busy: bool = False  # a partial exit order is in flight (position check must wait)
    sl_reason: str = "spot_stop_loss"  # "trailing_stop" once the strategy has moved the stop
    runner: RunnerState | None = None  # runner protection floor (TP strategies); shared with the ExitState
    entry_spot: float | None = None  # index spot at the entry signal (UI: SL risk / points running)

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
        self.recon_bad: dict[tuple[int, bool], set[str]] = {}  # tokens whose diff is a real mismatch
        self._recon_logged: dict[tuple[int, bool, str], float] = {}
        # manual exits at the broker, per (account, paper, token): when a manual SELL was seen, and how many
        # position polls in a row showed less than the platform's open trades hold
        self.partial_rearm_sec = 10.0  # a TP blocked by a manual order is re-armed after this
        self._ext_sell_at: dict[tuple[int, bool, str], datetime] = {}
        self._short_obs: dict[tuple[int, bool, str], int] = {}
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
    def open_trade_count(self, instance_id: int | None = None, account_id: int | None = None,
                         mode: str | None = None) -> int:
        """Open trades, optionally only one run's, one account's or one mode's (paper / live)."""
        return sum(1 for lt in self.live.values()
                   if (instance_id is None or lt.instance_id == instance_id)
                   and (account_id is None or lt.account_id == account_id)
                   and (mode is None or lt.mode == mode))

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
        """P&L of the quantity still held (at the current premium)."""
        ltp = self.market.ltp(lt.option_series)
        if ltp is None or not lt.open_qty:
            return 0.0
        return (ltp - lt.entry_price) * lt.open_qty

    @staticmethod
    def booked(lt: LiveTrade) -> float:
        """P&L already realised by TP1 / TP2 partial sells of a trade that is still open."""
        return sum((p["price"] - lt.entry_price) * p["filled"] for p in lt.partials
                   if p.get("filled") and p.get("price") is not None)

    @staticmethod
    def sold_value(lt: LiveTrade) -> float:
        return sum(p["price"] * p["filled"] for p in lt.partials if p.get("filled") and p.get("price") is not None)

    def open_pnl(self, lt: LiveTrade) -> float:
        """An open trade's whole P&L so far: partial sells already booked + the rest at the current premium.
        (The broker counts the booked part as realised the moment it fills; so must every total of ours.)"""
        return self.unrealized(lt) + self.booked(lt)

    def daily_pnl(self, day: date, instance_id: int | None = None, account_id: int | None = None) -> float:
        self._roll_day()
        total = 0.0
        for inst, acc, gross in self.realized_today.values():
            if (instance_id is None or inst == instance_id) and (account_id is None or acc == account_id):
                total += gross
        for lt in self.live.values():
            if (instance_id is None or lt.instance_id == instance_id) and (account_id is None or lt.account_id == account_id):
                total += self.open_pnl(lt)  # booked TP profit counts towards the daily limits at once
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
                              "partials": [_partial_plan(p, inst.lot_size) for p in plan.partial_targets],
                              **({"runner_plan": plan.runner} if plan.runner else {}),
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
            protective_pct=plan.protective_sl_pct,
            partials=[_partial_plan(p, inst.lot_size, entry_price, inst.tick_size) for p in plan.partial_targets],
            runner=runner_from_plan(plan.runner, entry_price, inst.tick_size), entry_spot=signal.spot)
        self.live[trade_id] = lt
        if lt.partials:
            self._save_meta(lt, partials=lt.partials)  # premium targets now have their level
        if lt.runner is not None:
            self._save_meta(lt, runner=lt.runner.as_dict())  # the trail % of the entry fill's band, fixed now
        self._save(lt, status="OPEN", quantity=filled, open_quantity=filled, entry_price=entry_price,
                   entry_time=lt.entry_time, entry_order_id=placed.order_id, tp_premium_level=lt.tp_premium_level)
        await self._place_protective(lt, adapter, sync)
        self._register_exit(lt)
        self._emit(Category.POSITION, "OPENED",
                   f"symbol={lt.symbol} qty={filled} avg={entry_price:.2f} spot_SL={_f(lt.spot_sl)} "
                   f"protective_sl={lt.protective_sl_order_id or '-'} TP={_f(lt.tp_spot_level or lt.tp_premium_level)} "
                   f"exit={hm_or_hms(lt.exit_at)}", lt=lt, symbol=lt.symbol, quantity=filled, average_price=entry_price,
                   spot_sl=lt.spot_sl, protective_sl_order_id=lt.protective_sl_order_id, tp=lt.tp_spot_level,
                   tp_premium=lt.tp_premium_level, exit_at=lt.exit_at, mode=lt.mode)
        self._emit(Category.STRATEGY, "ENTRY",
                   f"ENTRY {lt.direction} symbol={lt.symbol} strike={signal.option.strike:g} qty={filled} "
                   f"spot={signal.spot:.2f} spot_SL={_f(lt.spot_sl)} exit={hm_or_hms(lt.exit_at)}", lt=lt,
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
            last_premium=self.market.ltp(lt.option_series), partials=lt.partials, sl_reason=lt.sl_reason,
            runner=lt.runner))


    async def _place_protective(self, lt: LiveTrade, adapter: Any, sync: BrokerSync, qty: int | None = None) -> None:
        if not lt.protective_pct:
            return
        qty = lt.open_qty if qty is None else qty
        if qty <= 0:
            return
        trigger = round_to_tick(lt.entry_price * (1 - lt.protective_pct / 100), lt.tick_size, "down")
        if trigger < lt.tick_size:
            return
        inst = adapter.find_instrument(lt.exchange, lt.token) or adapter.resolve_symbol(lt.option_key)
        for order_type in (OrderType.SL_M, OrderType.SL):
            limit = round_to_tick(trigger * 0.95, lt.tick_size, "down") if order_type is OrderType.SL else None
            req = OrderRequest(instrument=inst, side=Side.SELL, quantity=qty, order_type=order_type,
                               product=lt.product, trigger_price=trigger, price=limit,
                               tag=make_tag(lt.instance_id or 0, lt.id, "PROTECTIVE_SL"))
            placed = await self.orders.place(adapter, sync, req, purpose="PROTECTIVE_SL", trade_db_id=lt.id,
                                             trade_ref=lt.ref, instance_id=lt.instance_id,
                                             instance_name=lt.instance_name, signal_price=trigger,
                                             lots=qty // max(1, lt.lot_size), attempts=2)
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
                       f"reason={reason} spot={_f(spot)} premium={_f(premium)}" + (f" by={by}" if by else "")
                       + _runner_txt(lt, reason),
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
            others = sum(t.open_qty for t in self.live.values() if t is not lt and t.status == "OPEN"
                         and t.token == lt.token and t.account_id == lt.account_id and t.mode == lt.mode)
            if pos is not None and fresh and not lt.mode == "paper":
                # several runs can hold the same strike: the others' open quantity is not ours to sell
                qty = max(0, min(qty, pos.net_quantity - others))
            # 3. a manual SELL order still working at the broker on this strike (e.g. a stop placed in the broker's
            # app) reserves that quantity: a sell of ours on top of it is a SHORT sale to the broker (rejected
            # for funds: 17-Nov-2026, 111 rejected orders in 4 minutes). Do not send orders; say which
            # order is in the way; the manual order's fill is then settled as a manual exit.
            manual = self._manual_sells(sync, lt.token)
            if manual and not lt.mode == "paper":
                held = pos.net_quantity if pos is not None else lt.open_qty + others
                reserved = sum(max(0, o.quantity - o.filled_quantity) for o in manual)
                free = max(0, held - reserved - others)
                if free < qty:
                    qty = free
                if qty <= 0:
                    return self._exit_blocked(lt, manual, reason, spot, premium)
            if qty <= 0:
                await self._finalize(lt, "manual_exit_at_broker", sync)
                return True
            inst = adapter.find_instrument(lt.exchange, lt.token) or adapter.resolve_symbol(lt.option_key)
            age = self.market.ltp_age(lt.option_key)
            if age is None or age > 5:
                await self.market.fetch_ltp(lt.option_key)  # paper fills and the log need a current premium
            remaining = qty
            refused: str | None = None
            for attempt in range(1, 4):
                req = OrderRequest(instrument=inst, side=Side.SELL, quantity=remaining, order_type=OrderType.MARKET,
                                   product=lt.product, tag=make_tag(lt.instance_id or 0, lt.id,
                                                                    "KILL" if reason == "kill_switch" else "EXIT"))
                placed = await self.orders.place(adapter, sync, req, purpose="EXIT", trade_db_id=lt.id,
                                                 trade_ref=lt.ref, instance_id=lt.instance_id,
                                                 instance_name=lt.instance_name, signal_price=premium,
                                                 lots=remaining // max(1, lt.lot_size))
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
                if final is not None and final.status is OrderStatus.REJECTED and _funds_rejection(final):
                    refused = final.status_message
                    break  # the broker sees a short sale: more orders now only pile up rejections
                if final is not None and not final.status.is_terminal:
                    await self.orders.cancel(adapter, sync, placed.order_id, trade_ref=lt.ref, reason="exit retry")
            if remaining > 0:
                lt.status = "OPEN"
                self._save(lt, status="OPEN", open_quantity=lt.open_qty)
                # refused for funds / margin: back off 5 s, 10 s, 20 s ... up to 60 s instead of 3 orders every 5 s
                lt.exit_rejects = lt.exit_rejects + 1 if refused else 0
                delay = min(60, 5 * 2 ** (lt.exit_rejects - 1)) if refused else 5
                why = (f" — the broker refused the sell for funds/margin, i.e. it sees no sellable quantity "
                       f"(a manual sell order or a square-off at the broker?): {refused}") if refused else ""
                self._emit(Category.ERROR, "EXIT_FAILED",
                           f"could not exit {remaining} units of {lt.symbol}; retrying in {delay}s — CHECK THE BROKER"
                           f"{why}", Level.CRITICAL, lt=lt, remaining=remaining, reason=reason, retry_sec=delay,
                           broker_reason=refused)
                st = self.exits.states.get(lt.id)
                loop = asyncio.get_running_loop()
                loop.call_later(delay, lambda: loop.create_task(
                    self.exit_trade(lt.id, reason, spot=spot, premium=premium)))
                if st is not None:
                    st.quantity = lt.open_qty
                return False
            await self._finalize(lt, reason, sync)
            return True

    def _partial_blocked(self, lt: LiveTrade, p: dict[str, Any], qty: int) -> bool:
        """A TP sell would be a short sale while manual SELL orders at the broker hold the quantity (see exit_trade):
        no order, the same one-time alert, and the target re-arms after 10 s (it sells on the next tick at or
        beyond its level once the manual order is gone)."""
        try:
            _, _, sync = self._route(lt)
        except Exception:
            return False  # the sell itself reports the missing route
        manual = self._manual_sells(sync, lt.token)
        if not manual:
            return False
        pos = sync.position_for(lt.token)
        others = sum(t.open_qty for t in self.live.values() if t is not lt and t.status == "OPEN"
                     and t.token == lt.token and t.account_id == lt.account_id and t.mode == lt.mode)
        held = pos.net_quantity if pos is not None else lt.open_qty + others
        reserved = sum(max(0, o.quantity - o.filled_quantity) for o in manual)
        if held - reserved - others >= qty:
            return False
        ids = ",".join(sorted(o.order_id for o in manual))
        if lt.blocked_by != ids:
            lt.blocked_by = ids
            desc = "; ".join(f"{o.order_id} SELL {o.quantity - o.filled_quantity} {o.order_type.value}"
                             + (f" trigger {o.trigger_price:g}" if o.trigger_price else "")
                             + (f" price {o.price:g}" if o.price else "") for o in manual)
            self._emit(Category.RISK, "EXIT_BLOCKED_BY_MANUAL_ORDER",
                       f"{p['label']}: cannot sell {qty} {lt.symbol} — manual order(s) at the broker hold the "
                       f"position ({desc}). No sell is sent (it would be a short sale); cancel the manual order "
                       f"and the target sells again while the price is at or beyond it", Level.CRITICAL, lt=lt,
                       reason=p["label"], orders=[o.order_id for o in manual])

        def rearm() -> None:
            if lt.id in self.live and not p.get("filled"):
                p["done"] = False

        asyncio.get_running_loop().call_later(self.partial_rearm_sec, rearm)
        return True

    @staticmethod
    def _manual_sells(sync: BrokerSync, token: str) -> list[Order]:
        """SELL orders on this strike still working at the broker that the platform did not place."""
        return [o for o in list(sync.orders.values()) if o.token == token and o.side is Side.SELL
                and o.status.is_working and parse_tag(o.tag) is None]

    def _exit_blocked(self, lt: LiveTrade, manual: list[Order], reason: str, spot: float | None,
                      premium: float | None) -> bool:
        """Our exit cannot be sold while manual sell orders hold the quantity: no orders, a clear alert (once per
        set of blocking orders), and a quiet re-check every 10 s. When the manual order fills, the trade is settled
        as a manual exit; when it is cancelled, the next re-check sells as usual."""
        lt.status = "OPEN"
        self._save(lt, status="OPEN")
        ids = ",".join(sorted(o.order_id for o in manual))
        if lt.blocked_by != ids:
            lt.blocked_by = ids
            desc = "; ".join(f"{o.order_id} SELL {o.quantity - o.filled_quantity} {o.order_type.value}"
                             + (f" trigger {o.trigger_price:g}" if o.trigger_price else "")
                             + (f" price {o.price:g}" if o.price else "") for o in manual)
            self._emit(Category.RISK, "EXIT_BLOCKED_BY_MANUAL_ORDER",
                       f"{reason}: cannot sell {lt.open_qty} {lt.symbol} — manual order(s) at the broker hold the "
                       f"position ({desc}). The platform sends no sell (it would be a short sale); cancel the manual "
                       f"order to let it exit, or the manual order's fill closes this trade", Level.CRITICAL, lt=lt,
                       reason=reason, orders=[o.order_id for o in manual])
        loop = asyncio.get_running_loop()
        loop.call_later(10, lambda: loop.create_task(self.exit_trade(lt.id, reason, spot=spot, premium=premium)))
        return False

    async def exit_partial(self, trade_db_id: int, idx: int, *, spot: float | None = None,
                           premium: float | None = None) -> bool:
        """Sell one scale-out target's quantity (TP1 / TP2) at market; the rest of the position stays open.

        The broker-side protective SL is shrunk to the quantity that will remain BEFORE the sell, so the
        stop can never sell more than we hold (which would leave a short option position).
        """
        lt = self.live.get(trade_db_id)
        if lt is None or not 0 <= idx < len(lt.partials):
            return False
        p = lt.partials[idx]
        async with lt.lock:
            if lt.status != "OPEN" or trade_db_id not in self.live:
                return False
            qty = min(int(p["qty"]), lt.open_qty)
            if qty <= 0:
                return False
            if lt.mode != "paper" and self._partial_blocked(lt, p, qty):
                return False
            lt.partial_busy = True
            try:
                self._emit(Category.EXIT, "PARTIAL_TRIGGERED",
                           f"reason={p['label']} level={_f(p['level'])} spot={_f(spot)} premium={_f(premium)} "
                           f"qty={qty} ({qty // max(1, lt.lot_size)} lots) of {lt.open_qty}", lt=lt,
                           reason=p["label"], target_level=p["level"], spot=spot, premium=premium, quantity=qty)
                try:
                    acc, adapter, sync = self._route(lt)
                except Exception as exc:
                    self._emit(Category.ERROR, "PARTIAL_EXIT_FAILED", f"no route to broker: {exc}", Level.ERROR, lt=lt)
                    return False
                await self._resize_protective(lt, adapter, sync, lt.open_qty - qty)
                inst = adapter.find_instrument(lt.exchange, lt.token) or adapter.resolve_symbol(lt.option_key)
                age = self.market.ltp_age(lt.option_key)
                if age is None or age > 5:
                    await self.market.fetch_ltp(lt.option_key)
                req = OrderRequest(instrument=inst, side=Side.SELL, quantity=qty, order_type=OrderType.MARKET,
                                   product=lt.product, tag=make_tag(lt.instance_id or 0, lt.id, "EXIT"))
                placed = await self.orders.place(adapter, sync, req, purpose="EXIT", trade_db_id=lt.id,
                                                 trade_ref=lt.ref, instance_id=lt.instance_id,
                                                 instance_name=lt.instance_name, signal_price=premium,
                                                 lots=qty // max(1, lt.lot_size))
                filled, price = 0, None
                if placed.order_id is not None:
                    lt.exit_order_ids.append(placed.order_id)
                    final = await self.orders.wait_terminal(sync, placed.order_id,
                                                            self.settings().sync.fill_timeout_sec)
                    if final is not None and not final.status.is_terminal:
                        final = await self.orders.cancel(adapter, sync, placed.order_id, trade_ref=lt.ref,
                                                         reason="partial exit not filled in time") or final
                    filled = final.filled_quantity if final else 0
                    if filled:
                        price = self._avg_fill(sync, [placed.order_id]) or (final.average_price if final else None)
                lt.open_qty = max(0, lt.open_qty - filled)
                p.update(done=True, filled=filled, price=price, spot=spot)
                self._save(lt, open_quantity=lt.open_qty)
                self._save_meta(lt, partials=lt.partials)
                st = self.exits.states.get(lt.id)
                if st is not None:
                    st.quantity = lt.open_qty
                if filled < qty:
                    # sell failed (fully or partly): put the stop back on what we actually hold
                    await self._resize_protective(lt, adapter, sync, lt.open_qty)
                    self._emit(Category.ERROR, "PARTIAL_EXIT_FAILED",
                               f"{p['label']}: sold {filled}/{qty}; {lt.open_qty} units stay open with the spot SL "
                               f"and timed exit", Level.ERROR, lt=lt, reason=p["label"], filled=filled, quantity=qty)
                    if filled == 0:
                        return False
                pnl = round((price - lt.entry_price) * filled, 2) if price is not None else None
                self._emit(Category.POSITION, "PARTIAL_EXIT",
                           f"{p['label'].upper()} sold qty={filled} price={_f(price)} pnl={_f(pnl)} "
                           f"remaining={lt.open_qty}", lt=lt, reason=p["label"], quantity=filled, price=price,
                           pnl=pnl, remaining=lt.open_qty)
                if lt.open_qty > 0:
                    self._runner_after_fill(lt)
                if lt.open_qty <= 0:
                    lt.status = "EXITING"
                    await self._finalize(lt, p["label"], sync)
                return True
            finally:
                lt.partial_busy = False

    def _runner_after_fill(self, lt: LiveTrade) -> None:
        """A TP sell is booked: arm the runner floor (start TP filled) or lift its step (a later TP filled)."""
        r = lt.runner
        if r is None:
            return
        event = runner_on_fill(r, lt.partials, lt.entry_price, self.clock.now())
        self._save_meta(lt, runner=r.as_dict())  # also keeps TP1's fill time for the timeout
        if event is None:
            return
        last = next((p["label"] for p in reversed(lt.partials) if p.get("filled")), "-")
        if event == "armed":
            self._emit(Category.EXIT, "RUNNER_FLOOR_ARMED",
                       f"start={r.start} step_floor={_f(r.step_floor)} trail={r.trail_pct:g}% peak={_f(r.peak)} "
                       f"floor={_f(r.floor)} qty={lt.open_qty}" + _runner_extra(r), lt=lt, start=r.start,
                       step_floor=r.step_floor, trail_pct=r.trail_pct, peak=r.peak, floor=r.floor,
                       exit_level=r.exit_level, timeout_at=r.timeout_at, quantity=lt.open_qty)
        else:
            self._emit(Category.EXIT, "RUNNER_FLOOR_RAISED",
                       f"after={last} step_floor={_f(r.step_floor)} peak={_f(r.peak)} floor={_f(r.floor)}", lt=lt,
                       step_floor=r.step_floor, peak=r.peak, floor=r.floor, after=last)
        st = self.exits.states.get(lt.id)
        if st is not None:  # the premium may already be at or below the new floor
            self.exits._check(st, self.clock.now())

    def runner_moved(self, st: ExitState, log_it: bool) -> None:
        """The trailing floor rose on an option tick: persist it (a restart keeps it); log it when up >= 0.5 %."""
        lt = self.live.get(st.trade_db_id)
        if lt is None or lt.runner is None:
            return
        r = lt.runner
        self._save_meta(lt, runner=r.as_dict())
        if log_it:
            self._emit(Category.EXIT, "RUNNER_FLOOR_RAISED", f"peak={_f(r.peak)} floor={_f(r.floor)}", Level.INFO,
                       lt=lt, peak=r.peak, floor=r.floor)

    def tighten_stop(self, trade_ref: str, level: float, *, instance_id: int | None = None) -> bool:
        """Move a trade's spot stop-loss closer to the price (trailing stop). It can only tighten: a level
        that would loosen the stop (lower for CE, higher for PE) is ignored."""
        lt = next((x for x in self.live.values()
                   if x.ref == trade_ref and (instance_id is None or x.instance_id == instance_id)), None)
        if lt is None or lt.status != "OPEN" or lt.spot_sl is None:
            return False
        level = round(level, 2)
        tighter = level > lt.spot_sl if lt.spot_sl_side == "below" else level < lt.spot_sl
        if not tighter:
            return False
        old, lt.spot_sl, lt.sl_reason = lt.spot_sl, level, "trailing_stop"
        st = self.exits.states.get(lt.id)
        if st is not None:
            st.spot_sl, st.sl_reason = level, "trailing_stop"
        self._save(lt, spot_sl=level)
        self._save_meta(lt, trailed=True)
        self._emit(Category.EXIT, "STOP_TRAILED", f"spot SL {old:.2f} -> {level:.2f}", Level.DEBUG, lt=lt,
                   old=old, new=level)
        if st is not None:  # the price may already be through the new level
            self.exits._check(st, self.clock.now())
        return True

    async def exit_by_ref(self, trade_ref: str, reason: str, *, instance_id: int | None = None,
                          partial: str | None = None) -> bool:
        """Exit asked for by a strategy (e.g. RSI reached its level on candle close): the whole trade, or
        with `partial` the lots of that scale-out target (TP1 / TP2) at market."""
        lt = next((x for x in self.live.values()
                   if x.ref == trade_ref and (instance_id is None or x.instance_id == instance_id)), None)
        if lt is None:
            return False
        spot = self.market.ltp(lt.spot_key) if lt.spot_key else None
        premium = self.market.ltp(lt.option_series)
        if partial is None:
            return await self.exit_trade(lt.id, reason, spot=spot, premium=premium)
        idx = next((i for i, p in enumerate(lt.partials) if p["label"] == partial and not p.get("done")), None)
        if idx is None:
            return False
        lt.partials[idx]["done"] = True
        return await self.exit_partial(lt.id, idx, spot=spot, premium=premium)

    async def _resize_protective(self, lt: LiveTrade, adapter: Any, sync: BrokerSync, qty: int) -> None:
        """Make the broker-side protective SL cover exactly `qty` units (cancel it at 0)."""
        if not lt.protective_sl_order_id:
            return
        sl = sync.orders.get(lt.protective_sl_order_id)
        if sl is not None and sl.status.is_terminal:
            return
        if qty <= 0:
            lt.cancelling_sl = True
            await self.orders.cancel(adapter, sync, lt.protective_sl_order_id, trade_ref=lt.ref,
                                     instance_id=lt.instance_id, reason="position fully scaled out")
            lt.cancelling_sl = False
            return
        from brokers.base import OrderModify

        inst = adapter.find_instrument(lt.exchange, lt.token) or adapter.resolve_symbol(lt.option_key)
        ok = await self.orders.modify(adapter, sync, OrderModify(
            order_id=lt.protective_sl_order_id, instrument=inst,
            order_type=sl.order_type if sl else OrderType.SL_M, quantity=qty, price=sl.price if sl else None,
            trigger_price=lt.protective_sl_trigger, product=lt.product), trade_ref=lt.ref)
        if ok:
            self._emit(Category.ORDER, "SL_RESIZED", f"protective SL {lt.protective_sl_order_id} quantity -> {qty}",
                       lt=lt, order_id=lt.protective_sl_order_id, quantity=qty)
            return
        # modify refused: replace the stop with a new one for the right quantity
        lt.cancelling_sl = True
        await self.orders.cancel(adapter, sync, lt.protective_sl_order_id, trade_ref=lt.ref,
                                 instance_id=lt.instance_id, reason="resize failed; replacing")
        lt.cancelling_sl = False
        lt.protective_sl_order_id = None
        await self._place_protective(lt, adapter, sync, qty)

    def _save_meta(self, lt: LiveTrade, **fields: Any) -> None:
        with self.db.session() as s:
            t = s.get(Trade, lt.id)
            if t is not None:
                t.meta = {**(t.meta or {}), **fields}

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
        parts = [p for p in lt.partials if p.get("filled")]
        part_txt = (" partials=" + ",".join(f"{p['label']}:{p['filled']}@{_f(p['price'])}" for p in parts)) if parts else ""
        self._emit(Category.STRATEGY, "EXIT",
                   f"EXIT reason={reason} premium_in={entry:.2f} premium_out={_f(out)} pnl={_f(gross)}{part_txt}"
                   f"{_runner_txt(lt, reason)}", lt=lt,
                   reason=reason, premium_in=entry, premium_out=out, pnl=gross,
                   partials=[{k: p[k] for k in ("label", "filled", "price")} for p in parts])
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
                # a confirmed manual SELL: the next position poll settles every affected trade at once
                self._ext_sell_at[(sync.account_id, sync.paper, o.token)] = self.clock.now()
                loop.create_task(sync.poll_once(full=True))

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
        by_token: dict[str, list[LiveTrade]] = {}
        for t in mine:
            by_token.setdefault(t.token, []).append(t)
        now = self.clock.now()
        for token, trades in by_token.items():
            key = (sync.account_id, sync.paper, token)
            if any(t.partial_busy or t.status == "EXITING" for t in trades):
                continue  # our own sell is in flight; quantities settle once it fills
            expected = sum(t.open_qty for t in trades if t.status == "OPEN")
            if expected <= 0:
                continue
            pos = sync.position_for(token)
            actual = max(0, pos.net_quantity if pos else 0)
            if actual >= expected:
                self._short_obs.pop(key, None)
                continue
            # trades younger than 5s may simply not be in the broker's positions yet
            settled = [t for t in trades if t.status == "OPEN"
                       and not (t.entry_time and (now - t.entry_time).total_seconds() < 5)]
            if not settled:
                continue
            seen = self._ext_sell_at.get(key)
            confirmed = seen is not None and (now - seen).total_seconds() < 120
            self._short_obs[key] = self._short_obs.get(key, 0) + 1
            # a shortfall is trusted at once after a manual SELL was seen, otherwise after two polls in a row
            if not confirmed and self._short_obs[key] < 2:
                continue
            self._short_obs.pop(key, None)
            self._ext_sell_at.pop(key, None)
            # the newest trades are closed first; every trade the sale covers is settled in this one pass
            short = expected - actual
            loop = asyncio.get_running_loop()
            for t in sorted(settled, key=lambda t: (t.entry_time or now, t.id), reverse=True):
                if short <= 0:
                    break
                keep = max(0, t.open_qty - short)
                short -= t.open_qty - keep
                loop.create_task(self._manual_exit(t.id, keep))
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

    def _order_just_filled(self, sync: BrokerSync, token: str, within_sec: float = 15) -> bool:
        """An order on this strike is filling, or filled moments ago: the broker's P&L is ahead of our books."""
        now = self.clock.now()
        for o in list(sync.orders.values()):
            if o.token != token or o.status in (OrderStatus.TRIGGER_PENDING, OrderStatus.REJECTED,
                                                OrderStatus.CANCELLED):
                continue  # a resting stop-loss does not move the P&L
            if o.status is not OrderStatus.COMPLETE:
                return True
            for t in (o.exchange_time, o.placed_at):
                if t is None:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=now.tzinfo)
                if abs((now - t).total_seconds()) < within_sec:
                    return True
        return False

    def _reconcile_pnl(self, sync: BrokerSync, positions: list[Position]) -> None:
        """Compare platform P&L with the broker's per-symbol P&L where all activity is ours."""
        today = self.clock.now().date()
        key = (sync.account_id, sync.paper)
        diffs: dict[str, float] = {}
        bad: set[str] = set()
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
            busy = False
            for lt in self.live.values():
                if lt.token == pos.token and lt.account_id == sync.account_id and (lt.mode == "paper") == sync.paper:
                    involved = True
                    busy = busy or lt.partial_busy or lt.status == "EXITING"
                    ltp = pos.ltp or self.market.ltp(lt.option_series) or lt.entry_price
                    platform += (ltp - lt.entry_price) * lt.open_qty + self.booked(lt)
            if not involved:
                continue
            if busy or self._order_just_filled(sync, pos.token):
                continue  # an order of ours is filling / being booked: the broker is a moment ahead, not a mismatch
            diff = round(platform - pos.pnl, 2)
            diffs[pos.token] = diff
            # The broker works out a line's P&L from its buy / sell AVERAGE prices rounded to 2 decimals, so with
            # fills at mixed prices it is off by up to 0.005 x quantity per side (e.g. 6.50 on 975 + 975 units).
            # Our figure is from the exact fills; that rounding is not a mismatch.
            rounding = 0.01 * (pos.buy_quantity + pos.sell_quantity) / 2 + 0.01
            if abs(diff) > max(tol, rounding):
                bad.add(pos.token)
                lk = (sync.account_id, sync.paper, pos.token)
                # once per distinct difference (not every minute, all evening, for the same closed figure)
                if self._recon_logged.get(lk) != diff:
                    self._recon_logged[lk] = diff
                    self.bus.emit(Category.RISK, "PNL_MISMATCH",
                                  f"{pos.tradingsymbol}: platform {platform:.2f} vs broker {pos.pnl:.2f} (diff {diff:+.2f})",
                                  Level.WARNING, account_id=sync.account_id, account_label=sync.account_label,
                                  platform=round(platform, 2), broker=pos.pnl, diff=diff, symbol=pos.tradingsymbol)
        self.recon[key] = diffs
        self.recon_bad[key] = bad

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
                      f"resumed {lt.symbol} qty={lt.open_qty} avg={lt.entry_price:.2f} exit={hm_or_hms(lt.exit_at)}",
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
            protective_sl_status=row.protective_sl_status, status="OPEN", exit_order_ids=exit_ids,
            partials=[dict(p) for p in meta.get("partials") or []],
            sl_reason="trailing_stop" if meta.get("trailed") else "spot_stop_loss",
            runner=(RunnerState.from_dict(meta.get("runner"))
                    or runner_from_plan(meta.get("runner_plan"), row.entry_price or 0.0,
                                        float(meta.get("tick_size", 0.05)))),
            entry_spot=row.signal_spot)

    def today_trade_infos(self, instance_id: int, day: date | None = None) -> list[TradeInfo]:
        """The instance's stored trades of `day` (default today) for Strategy.restore()."""
        today = day or self.clock.now().date()
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
            slv = sl_view(lt.direction, lt.entry_spot, lt.spot_sl, lt.spot_sl_side, spot)
            sl_dist = slv.get("sl_left")
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
            booked = self.booked(lt)
            charges = option_charges(self.settings().charges, lt.entry_price * lt.entry_qty,
                                     (ltp or lt.entry_price) * lt.open_qty + self.sold_value(lt)).total
            out.append({
                "id": lt.id, "trade_ref": lt.ref, "instance_id": lt.instance_id, "instance_name": lt.instance_name,
                "account_id": lt.account_id, "account_label": lt.account_label, "mode": lt.mode,
                "direction": lt.direction, "symbol": lt.symbol, "qty": lt.open_qty, "lots": lt.lots,
                "entry_price": lt.entry_price, "ltp": ltp, "unrealized": unreal, "booked": round(booked, 2),
                "net_estimate": round(unreal + booked - charges, 2) if unreal is not None else None,
                "spot": spot, "spot_sl": lt.spot_sl, "spot_sl_side": lt.spot_sl_side, "sl_distance": sl_dist,
                "entry_spot": lt.entry_spot, "sl_risk": slv.get("sl_risk"), "sl_left_pct": slv.get("sl_left_pct"),
                "points_running": slv.get("running"),
                "protective_sl_order_id": lt.protective_sl_order_id, "protective_sl_trigger": lt.protective_sl_trigger,
                "protective_sl_status": lt.protective_sl_status,
                "tp_level": lt.tp_spot_level if lt.tp_spot_level is not None else lt.tp_premium_level,
                "tp_kind": "spot" if lt.tp_spot_level is not None else ("premium" if lt.tp_premium_level else None),
                "tp_distance": tp_dist, "exit_at": lt.exit_at.isoformat(),
                "runner": lt.runner.text() if lt.runner is not None else None,
                "seconds_to_exit": max(0, int((lt.exit_at - now).total_seconds())), "status": lt.status,
                "broker_pnl": broker_pnl, "pnl_diff": diff,
                "mismatch": lt.token in self.recon_bad.get((lt.account_id, lt.mode == "paper"), set()),
            })
        return out


_FUNDS_WORDS = ("insufficient fund", "margin", "rms:", "rms ", "short sell", "not enough")


def _funds_rejection(o: Order) -> bool:
    """A rejection meaning the broker would treat this sell as a new (short) position."""
    msg = (o.status_message or "").lower()
    return any(w in msg for w in _FUNDS_WORDS)


def _partial_plan(p: Any, lot_size: int, entry_price: float | None = None, tick_size: float = 0.05) -> dict[str, Any]:
    """A scale-out target as stored on the trade. Premium targets get their level from the entry fill."""
    if getattr(p, "premium_bands", None):
        pct = band_pct(p.premium_bands, entry_price) if entry_price else None
        level = round_to_tick(entry_price * (1 + pct / 100), tick_size, "up") if pct is not None else None
        return {"label": p.label, "basis": "premium", "pct": pct, "level": level, "side": "above",
                "qty": p.lots * lot_size, "done": False, "filled": 0, "price": None, "spot": None}
    if p.premium_pct is not None:
        level = (round_to_tick(entry_price * (1 + p.premium_pct / 100), tick_size, "up")
                 if entry_price else None)
        return {"label": p.label, "basis": "premium", "pct": p.premium_pct, "level": level, "side": "above",
                "qty": p.lots * lot_size, "done": False, "filled": 0, "price": None, "spot": None}
    return {"label": p.label, "basis": "spot", "level": p.spot_level, "side": p.side, "qty": p.lots * lot_size,
            "done": False, "filled": 0, "price": None, "spot": None}


def _runner_txt(lt: LiveTrade, reason: str) -> str:
    r = lt.runner
    if not reason.startswith("runner_") or r is None:
        return ""
    return f" floor={_f(r.floor)} peak={_f(r.peak)}" + _runner_extra(r)


def _runner_extra(r: RunnerState) -> str:
    out = f" exit_level={_f(r.exit_level)} (buffer {r.buffer_pct:g}%)" if r.buffer_pct else ""
    return out + (f" timeout={r.timeout_at:%H:%M:%S}" if r.timeout_at is not None else "")


def _f(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"
