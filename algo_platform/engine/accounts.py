"""Broker account runtimes and real-time broker sync.

The broker is the source of truth: every order the platform shows comes from the broker's
order-update stream, backed up by polling the order book, trade book and positions. Every
platform order carries an order tag (instance + trade + purpose) so any broker order can be
mapped back; anything else is shown as External / manual.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select

from brokers.base import BrokerAdapter, BrokerError, Fill, Order, OrderStatus, Position, Side
from brokers.paper import PaperBroker, PaperCredentials
from brokers.ratelimit import retry_async
from brokers.registry import create_adapter, get_broker_class, is_stub
from core.clock import Clock
from core.events import Category, EventBus, Level
from core.settings_schema import AccountRiskLimits
from storage.db import Database
from storage.models import BrokerAccount, FillRecord, OrderRecord
from storage.repo import account_credentials, load_paper_state, save_paper_state

log = logging.getLogger("algo.accounts")

TAG_RE = re.compile(r"^I(\d+)T(\d+)([ESXK])$")
PURPOSE_BY_CHAR = {"E": "ENTRY", "S": "PROTECTIVE_SL", "X": "EXIT", "K": "KILL"}
CHAR_BY_PURPOSE = {v: k for k, v in PURPOSE_BY_CHAR.items()}


def make_tag(instance_id: int, trade_db_id: int, purpose: str) -> str:
    return f"I{instance_id}T{trade_db_id}{CHAR_BY_PURPOSE[purpose]}"


def parse_tag(tag: str | None) -> tuple[int, int, str] | None:
    if not tag:
        return None
    m = TAG_RE.match(tag.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), PURPOSE_BY_CHAR[m.group(3)]


STATUS_EVENT = {
    OrderStatus.OPEN: "OPEN",
    OrderStatus.TRIGGER_PENDING: "TRIGGER_PENDING",
    OrderStatus.PARTIALLY_FILLED: "PARTIAL_FILL",
    OrderStatus.COMPLETE: "FILLED",
    OrderStatus.REJECTED: "REJECTED",
    OrderStatus.CANCELLED: "CANCELLED",
}

OrderListener = Callable[["BrokerSync", Order, Order | None, OrderRecord], None]
PositionsListener = Callable[["BrokerSync", list[Position]], None]


@dataclass
class SyncHealth:
    last_poll_at: datetime | None = None
    last_stream_event_at: datetime | None = None
    last_error: str | None = None
    polls: int = 0


class BrokerSync:
    """Keeps the platform's view of one adapter's orders/positions/fills equal to the broker's."""

    def __init__(self, adapter: BrokerAdapter, account_id: int, account_label: str, paper: bool, db: Database,
                 bus: EventBus, clock: Clock, poll_interval: Callable[[], float]) -> None:
        self.adapter = adapter
        self.account_id = account_id
        self.account_label = account_label
        self.paper = paper
        self.db = db
        self.bus = bus
        self.clock = clock
        self.poll_interval = poll_interval
        self.orders: dict[str, Order] = {}
        self.positions: list[Position] = []
        self.positions_at: datetime | None = None
        self.fills: dict[tuple[str, str], Fill] = {}
        self.health = SyncHealth()
        self._waiters: dict[str, list[asyncio.Event]] = {}
        self.order_listeners: list[OrderListener] = []
        self.positions_listeners: list[PositionsListener] = []
        self._task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()
        adapter.on_order_update(self._on_stream_order)

    @property
    def name(self) -> str:
        return f"{self.account_label}{' (paper)' if self.paper and not self.adapter.is_paper else ''}"

    def _emit(self, category: Category, event_type: str, message: str, level: Level = Level.INFO,
              trade_id: str | None = None, instance_id: int | None = None, **payload: Any) -> None:
        self.bus.emit(category, event_type, message, level, account_id=self.account_id,
                      account_label=self.account_label, trade_id=trade_id, instance_id=instance_id,
                      paper=self.paper, **payload)

    # ------------------------------------------------------------------ order intake
    def _on_stream_order(self, order: Order) -> None:
        self.health.last_stream_event_at = self.clock.now()
        self.handle_order(order, "stream")

    def handle_order(self, o: Order, source: str = "poll") -> None:
        prev = self.orders.get(o.order_id)
        if prev is not None:
            # never let a stale poll response move an order backwards
            if prev.status.is_terminal and not o.status.is_terminal:
                return
            if o.filled_quantity < prev.filled_quantity:
                return
            changed = (prev.status != o.status or prev.filled_quantity != o.filled_quantity
                       or prev.trigger_price != o.trigger_price or prev.price != o.price
                       or prev.quantity != o.quantity or prev.order_type != o.order_type)
            if not changed:
                return
        self.orders[o.order_id] = o
        rec = self._persist(o, prev)
        self._log_change(o, prev, rec, source)
        for cb in list(self.order_listeners):
            try:
                cb(self, o, prev, rec)
            except Exception:
                log.exception("order listener failed")
        for ev in self._waiters.get(o.order_id, []):
            ev.set()

    def _persist(self, o: Order, prev: Order | None) -> OrderRecord:
        with self.db.session() as s:
            rec = s.scalar(select(OrderRecord).where(OrderRecord.account_id == self.account_id,
                                                     OrderRecord.paper == self.paper,
                                                     OrderRecord.broker_order_id == o.order_id))
            if rec is None:
                from storage.models import StrategyInstance, Trade

                parsed = parse_tag(o.tag)
                t = s.get(Trade, parsed[1]) if parsed and parsed[1] else None
                inst = s.get(StrategyInstance, parsed[0]) if parsed and parsed[0] else None
                rec = OrderRecord(account_id=self.account_id, paper=self.paper, broker_order_id=o.order_id,
                                  tag=o.tag, exchange=o.exchange.value, tradingsymbol=o.tradingsymbol, token=o.token,
                                  side=o.side.value, order_type=o.order_type.value, product=o.product.value,
                                  quantity=o.quantity, status=o.status.value, placed_at=o.placed_at or self.clock.now(),
                                  is_external=parsed is None, purpose=parsed[2] if parsed else "EXTERNAL",
                                  instance_id=inst.id if inst else None, trade_id=t.id if t else None,
                                  trade_ref=t.trade_ref if t else None)
                s.add(rec)
            rec.status = o.status.value
            rec.status_message = o.status_message or rec.status_message
            rec.filled_quantity = o.filled_quantity
            rec.average_price = o.average_price
            rec.price = o.price
            rec.trigger_price = o.trigger_price
            rec.quantity = o.quantity
            rec.order_type = o.order_type.value
            rec.exchange_order_id = o.exchange_order_id or rec.exchange_order_id
            rec.exchange_time = o.exchange_time or rec.exchange_time
            rec.updated_at = self.clock.now()
            s.flush()
            s.expunge(rec)
            return rec

    def _log_change(self, o: Order, prev: Order | None, rec: OrderRecord, source: str) -> None:
        trade_ref, inst_id = rec.trade_ref, rec.instance_id
        if rec.is_external and prev is None:
            self._emit(Category.ORDER, "EXTERNAL_ORDER",
                       f"external/manual order {o.order_id} {o.side.value} {o.quantity} {o.tradingsymbol} "
                       f"{o.order_type.value} status={o.status.value}", Level.WARNING,
                       order_id=o.order_id, symbol=o.tradingsymbol, side=o.side.value, qty=o.quantity,
                       status=o.status.value, source=source)
            return
        if prev is not None and prev.status == o.status and (
                prev.trigger_price != o.trigger_price or prev.price != o.price or prev.quantity != o.quantity
                or prev.order_type != o.order_type):
            changes = {k: [getattr(prev, k), getattr(o, k)] for k in ("price", "trigger_price", "quantity", "order_type")
                       if getattr(prev, k) != getattr(o, k)}
            self._emit(Category.ORDER, "MODIFIED",
                       f"order_id={o.order_id} " + " ".join(f"{k}={v[0]}→{v[1]}" for k, v in changes.items()),
                       trade_id=trade_ref, instance_id=inst_id, order_id=o.order_id,
                       changes={k: [str(a), str(b)] for k, (a, b) in changes.items()}, source=source)
            return
        if prev is not None and prev.status == o.status and prev.filled_quantity == o.filled_quantity:
            return
        etype = STATUS_EVENT.get(o.status, o.status.value)
        level = Level.ERROR if o.status is OrderStatus.REJECTED else Level.INFO
        msg = f"order_id={o.order_id} side={o.side.value} symbol={o.tradingsymbol} status={o.status.value}"
        payload: dict[str, Any] = {"order_id": o.order_id, "status": o.status.value, "source": source,
                                   "purpose": rec.purpose}
        if o.status in (OrderStatus.COMPLETE, OrderStatus.PARTIALLY_FILLED):
            slip = None
            if rec.signal_price:
                slip = round((o.average_price - rec.signal_price) * (1 if o.side is Side.BUY else -1), 2)
            msg = (f"order_id={o.order_id} side={o.side.value} fill_price={o.average_price:.2f} "
                   f"qty={o.filled_quantity}/{o.quantity} exch_order_id={o.exchange_order_id or '-'}"
                   + (f" slippage={slip:+.2f}" if slip is not None else ""))
            payload.update(fill_price=o.average_price, fill_qty=o.filled_quantity, exch_order_id=o.exchange_order_id,
                           exch_time=o.exchange_time, slippage=slip, signal_price=rec.signal_price)
        elif o.status is OrderStatus.REJECTED:
            msg += f" reason={o.status_message!r}"
            payload["reason"] = o.status_message
        elif o.status is OrderStatus.TRIGGER_PENDING:
            msg += f" trigger={o.trigger_price}"
        self._emit(Category.ORDER, etype, msg, level, trade_id=trade_ref, instance_id=inst_id, **payload)

    # ------------------------------------------------------------------ waiting
    async def wait_for(self, order_id: str, predicate: Callable[[Order], bool], timeout: float) -> Order | None:
        o = self.orders.get(order_id)
        if o is not None and predicate(o):
            return o
        ev = asyncio.Event()
        self._waiters.setdefault(order_id, []).append(ev)
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return self.orders.get(order_id)
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(remaining, 2.0))
                except asyncio.TimeoutError:
                    # stream may have missed it: ask the broker directly
                    await self.poll_orders()
                o = self.orders.get(order_id)
                if o is not None and predicate(o):
                    return o
        finally:
            lst = self._waiters.get(order_id, [])
            if ev in lst:
                lst.remove(ev)
            if not lst:
                self._waiters.pop(order_id, None)

    # ------------------------------------------------------------------ polling
    async def poll_orders(self) -> None:
        try:
            for o in await self.adapter.get_order_book():
                self.handle_order(o, "poll")
        except Exception as exc:
            self.health.last_error = str(exc)

    async def poll_once(self, full: bool = True) -> None:
        if not self.adapter.is_connected():
            return
        async with self._poll_lock:
            try:
                for o in await self.adapter.get_order_book():
                    self.handle_order(o, "poll")
                if full:
                    self._handle_fills(await self.adapter.get_trade_book())
                    self.positions = await self.adapter.get_positions()
                    self.positions_at = self.clock.now()
                    for cb in list(self.positions_listeners):
                        try:
                            cb(self, self.positions)
                        except Exception:
                            log.exception("positions listener failed")
                self.health.last_poll_at = self.clock.now()
                self.health.polls += 1
                self.health.last_error = None
            except BrokerError as exc:
                self.health.last_error = str(exc)
                self._emit(Category.ERROR, "SYNC_POLL_FAILED", f"{self.name}: {exc}", Level.WARNING)
            except Exception as exc:
                self.health.last_error = str(exc)
                log.exception("poll failed")

    def _handle_fills(self, fills: list[Fill]) -> None:
        new = [f for f in fills if (f.fill_id, f.order_id) not in self.fills]
        if not new:
            return
        with self.db.session() as s:
            for f in new:
                self.fills[(f.fill_id, f.order_id)] = f
                exists = s.scalar(select(FillRecord.id).where(FillRecord.account_id == self.account_id,
                                                              FillRecord.fill_id == f.fill_id,
                                                              FillRecord.broker_order_id == f.order_id))
                if exists:
                    continue
                rec = s.scalar(select(OrderRecord).where(OrderRecord.account_id == self.account_id,
                                                         OrderRecord.paper == self.paper,
                                                         OrderRecord.broker_order_id == f.order_id))
                s.add(FillRecord(account_id=self.account_id, paper=self.paper, fill_id=f.fill_id,
                                 broker_order_id=f.order_id, trade_id=rec.trade_id if rec else None,
                                 tradingsymbol=f.tradingsymbol, side=f.side.value, price=f.price,
                                 quantity=f.quantity, time=f.time))

    def fills_for(self, order_id: str) -> list[Fill]:
        return [f for (fid, oid), f in self.fills.items() if oid == order_id]

    def position_for(self, token: str, product: str | None = None) -> Position | None:
        for p in self.positions:
            if p.token == token and (product is None or p.product.value == product):
                return p
        return None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name=f"sync-{self.name}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once(full=True)
                await asyncio.sleep(self.poll_interval())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync loop error")
                await asyncio.sleep(5)


class AccountRuntime:
    def __init__(self, row: BrokerAccount, db: Database, bus: EventBus, clock: Clock,
                 poll_interval: Callable[[], float], cache_dir: Any, price_source: Callable[..., Any] | None = None):
        self.id = row.id
        self.label = row.label
        self.broker_type = row.broker_type
        self.db = db
        self.bus = bus
        self.clock = clock
        self.poll_interval = poll_interval
        self.auto_relogin = row.auto_relogin
        self.kill_switch = row.kill_switch
        self.risk = AccountRiskLimits.model_validate(row.risk_limits or {})
        self.stub = is_stub(row.broker_type)
        self.status = "DISCONNECTED"
        self.last_error: str | None = None
        self.last_login_at: datetime | None = None
        self.funds: float | None = row.funds_available
        self.streams: dict[str, str] = {}
        self.ready = asyncio.Event()
        self.price_source = price_source
        creds = account_credentials(row)
        extra = {"clock": clock} if issubclass(get_broker_class(row.broker_type), PaperBroker) else {}
        self.adapter: BrokerAdapter = create_adapter(row.broker_type, creds, account_id=row.id, label=row.label,
                                                     cache_dir=cache_dir, **extra)
        if isinstance(self.adapter, PaperBroker) and self.adapter.broker_type == "paper":
            self.adapter.supports_tick_stream = self.adapter.creds.simulate_market  # type: ignore[misc]
            self.adapter.price_source = price_source
            self._attach_paper_persistence(self.adapter, f"account:{self.id}")
        self.adapter.on_connection_change(self._on_stream_state)
        self.paper_shadow: PaperBroker | None = None
        self.live_sync = BrokerSync(self.adapter, self.id, self.label, self.adapter.is_paper, db, bus, clock,
                                    poll_interval)
        self.paper_sync: BrokerSync | None = None
        self._relogin_lock = asyncio.Lock()

    @property
    def is_paper_account(self) -> bool:
        return self.adapter.is_paper

    def _emit(self, category: Category, event_type: str, message: str, level: Level = Level.INFO,
              **payload: Any) -> None:
        self.bus.emit(category, event_type, message, level, account_id=self.id, account_label=self.label, **payload)

    def _attach_paper_persistence(self, paper: PaperBroker, key: str) -> None:
        state = load_paper_state(self.db, key)
        if state:
            paper.restore(state)
        pending = {"flag": False}

        def schedule() -> None:
            if pending["flag"]:
                return
            pending["flag"] = True

            def write() -> None:
                pending["flag"] = False
                try:
                    save_paper_state(self.db, key, paper.snapshot())
                except Exception:
                    log.exception("paper state save failed")

            try:
                asyncio.get_running_loop().call_later(0.5, write)
            except RuntimeError:
                write()

        paper.state_listeners.append(schedule)

    def _on_stream_state(self, stream: str, state: str, detail: str) -> None:
        prev = self.streams.get(stream)
        self.streams[stream] = state
        if prev == state and state not in ("error",):
            return
        name = {"ticks": "TICK_WS", "orders": "ORDER_WS"}.get(stream, stream.upper())
        level = Level.INFO if state in ("connected", "resubscribed") else Level.WARNING
        self._emit(Category.SYSTEM, f"{name}_{state.upper()}", f"{self.label}: {stream} stream {state} {detail}".strip(),
                   level, stream=stream, state=state, detail=detail)
        if stream == "orders" and state == "connected" and prev in ("reconnecting", "error", "disconnected"):
            # full reconciliation after an order-stream reconnect
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.full_sync())
            except RuntimeError:
                pass

    def shadow(self) -> tuple[PaperBroker, BrokerSync]:
        """Paper broker that mirrors this (real) account for paper-mode instances."""
        if self.adapter.is_paper:
            assert isinstance(self.adapter, PaperBroker)
            return self.adapter, self.live_sync
        if self.paper_shadow is None:
            self.paper_shadow = PaperBroker(PaperCredentials(), delegate=self.adapter, clock=self.clock,
                                            price_source=self.price_source, account_id=self.id,
                                            label=f"{self.label} (paper)")
            self._attach_paper_persistence(self.paper_shadow, f"shadow:{self.id}")
            self.paper_sync = BrokerSync(self.paper_shadow, self.id, self.label, True, self.db, self.bus, self.clock,
                                         self.poll_interval)
        assert self.paper_sync is not None
        return self.paper_shadow, self.paper_sync

    def order_route(self, mode: str) -> tuple[BrokerAdapter, BrokerSync]:
        if mode == "live":
            if self.adapter.is_paper:
                raise BrokerError(f"{self.label} is a paper account; live mode needs a real broker account")
            return self.adapter, self.live_sync
        return self.shadow()

    def all_syncs(self) -> list[BrokerSync]:
        out = [self.live_sync]
        if self.paper_sync is not None:
            out.append(self.paper_sync)
        return out

    async def connect(self) -> bool:
        if self.stub:
            self.status = "UNSUPPORTED"
            self.last_error = "broker adapter not implemented yet (stub)"
            return False
        async with self._relogin_lock:
            self.status = "CONNECTING"
            try:
                profile = await retry_async(self.adapter.login, attempts=3, base_delay=2.0,
                                            is_retryable=lambda e: getattr(e, "retryable", True))
            except Exception as exc:
                self.status, self.last_error = "ERROR", str(exc)
                self._emit(Category.SYSTEM, "BROKER_LOGIN_FAILED", f"{self.label}: {exc}", Level.ERROR)
                self._save_status()
                return False
            self.status, self.last_error = "CONNECTED", None
            self.last_login_at = self.clock.now()
            self._emit(Category.SYSTEM, "BROKER_LOGIN", f"{self.label}: logged in as {profile.client_id}",
                       client_id=profile.client_id)
            t0 = self.clock.now()
            try:
                n = await self.adapter.load_instruments()
                if n:
                    secs = (self.clock.now() - t0).total_seconds()
                    self._emit(Category.SYSTEM, "INSTRUMENTS_LOADED", f"{self.label}: {n} instruments in {secs:.1f}s",
                               count=n, seconds=round(secs, 2))
            except Exception as exc:
                self._emit(Category.ERROR, "INSTRUMENTS_FAILED", f"{self.label}: {exc}", Level.ERROR)
            try:
                f = await self.adapter.get_funds()
                self.funds = f.available_cash
            except Exception:
                pass
            if self.paper_shadow is not None:
                await self.paper_shadow.login()
            self._save_status()
            return True

    async def relogin(self) -> bool:
        self._emit(Category.SYSTEM, "SESSION_REFRESH", f"{self.label}: scheduled re-login")
        return await self.connect()

    async def full_sync(self) -> None:
        for sync in self.all_syncs():
            await sync.poll_once(full=True)
        self._emit(Category.SYSTEM, "RECONCILED", f"{self.label}: full order/position sync complete", Level.DEBUG)

    def start_sync(self) -> None:
        self.live_sync.start()
        if self.paper_sync is not None:
            self.paper_sync.start()

    async def ensure_shadow_started(self) -> None:
        paper, sync = self.shadow()
        if not paper.is_connected():
            await paper.login()
        if sync._task is None and self.status == "CONNECTED":
            sync.start()

    async def refresh_funds(self) -> None:
        try:
            self.funds = (await self.adapter.get_funds()).available_cash
            self._save_status()
        except Exception as exc:
            self.last_error = str(exc)

    def _save_status(self) -> None:
        with self.db.session() as s:
            row = s.get(BrokerAccount, self.id)
            if row is not None:
                row.status = self.status
                row.last_error = self.last_error
                row.last_login_at = self.last_login_at or row.last_login_at
                row.funds_available = self.funds

    async def close(self) -> None:
        for sync in self.all_syncs():
            await sync.stop()
        try:
            await self.adapter.close()
        except Exception:
            pass
        if self.paper_shadow is not None:
            await self.paper_shadow.close()
        self.status = "DISCONNECTED"

    def indicator(self, tick_stale: bool) -> str:
        """green / amber / red connection indicator for the UI."""
        if self.status != "CONNECTED":
            return "red"
        h = self.live_sync.health
        if h.last_error or tick_stale or self.streams.get("orders") in ("reconnecting", "error"):
            return "amber"
        return "green"

    def snapshot(self) -> dict[str, Any]:
        h = self.live_sync.health
        return {
            "id": self.id, "label": self.label, "broker_type": self.broker_type, "status": self.status,
            "last_error": self.last_error, "funds": self.funds, "kill_switch": self.kill_switch,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "last_order_sync_at": h.last_poll_at.isoformat() if h.last_poll_at else None,
            "last_stream_event_at": h.last_stream_event_at.isoformat() if h.last_stream_event_at else None,
            "streams": dict(self.streams), "is_paper": self.adapter.is_paper,
        }
