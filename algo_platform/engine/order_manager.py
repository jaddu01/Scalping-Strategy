"""Order placement with full request/response logging, safe retries and fill confirmation.

Retries: a retryable error (network/timeout) may mean the broker *did* accept the order, so
before every retry the order book is checked for an order carrying our tag; only if none
exists is the order sent again. Broker rejections are never retried blindly.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from brokers.base import (BrokerAdapter, BrokerError, Order, OrderModify, OrderRejected, OrderRequest, OrderStatus,
                          OrderType)
from core.clock import Clock
from core.events import Category, EventBus, Level
from engine.accounts import BrokerSync
from storage.db import Database
from storage.models import OrderRecord

log = logging.getLogger("algo.orders")


@dataclass
class PlacedOrder:
    order_id: str | None
    record_id: int | None
    error: str | None = None
    latency_ms: int | None = None


class OrderManager:
    def __init__(self, db: Database, bus: EventBus, clock: Clock) -> None:
        self.db = db
        self.bus = bus
        self.clock = clock

    def _emit(self, sync: BrokerSync, event_type: str, message: str, level: Level = Level.INFO,
              trade_ref: str | None = None, instance_id: int | None = None, instance_name: str | None = None,
              **payload: Any) -> None:
        self.bus.emit(Category.ORDER, event_type, message, level, account_id=sync.account_id,
                      account_label=sync.account_label, trade_id=trade_ref, instance_id=instance_id,
                      instance_name=instance_name, paper=sync.paper, **payload)

    async def place(self, adapter: BrokerAdapter, sync: BrokerSync, req: OrderRequest, *, purpose: str,
                    trade_db_id: int | None, trade_ref: str | None, instance_id: int | None,
                    instance_name: str | None, signal_price: float | None = None, lots: int | None = None,
                    attempts: int = 3) -> PlacedOrder:
        inst = req.instrument
        variety = "STOPLOSS" if req.order_type in (OrderType.SL, OrderType.SL_M) else "NORMAL"
        lots_txt = f"{lots} lots ({req.quantity} units)" if lots else f"{req.quantity} units"
        self._emit(
            sync, "REQUEST",
            f"variety={variety} symbol={inst.tradingsymbol} token={inst.token} exch={inst.exchange.value} "
            f"side={req.side.value} type={req.order_type.value} product={req.product.value} "
            f"validity={req.validity.value} qty={lots_txt} price={req.price or 0} trigger={req.trigger_price or 0} "
            f"tag={req.tag} purpose={purpose}",
            trade_ref=trade_ref, instance_id=instance_id, instance_name=instance_name,
            variety=variety, tradingsymbol=inst.tradingsymbol, token=inst.token, exchange=inst.exchange.value,
            side=req.side.value, order_type=req.order_type.value, product=req.product.value,
            validity=req.validity.value, lots=lots, quantity=req.quantity, price=req.price,
            trigger_price=req.trigger_price, order_tag=req.tag, signal_price=signal_price, purpose=purpose)
        last_err: str | None = None
        for attempt in range(1, attempts + 1):
            t0 = _time.perf_counter()
            try:
                order_id = await adapter.place_order(req)
                latency = int((_time.perf_counter() - t0) * 1000)
                rec_id = self._record(sync, order_id, req, purpose, trade_db_id, trade_ref, instance_id, signal_price,
                                      latency)
                self._emit(sync, "ACCEPTED", f"order_id={order_id} latency={latency}ms", trade_ref=trade_ref,
                           instance_id=instance_id, instance_name=instance_name, order_id=order_id,
                           latency_ms=latency, attempt=attempt, purpose=purpose)
                return PlacedOrder(order_id, rec_id, None, latency)
            except OrderRejected as exc:
                latency = int((_time.perf_counter() - t0) * 1000)
                self._emit(sync, "REJECTED", f"broker rejected order: {exc}", Level.ERROR, trade_ref=trade_ref,
                           instance_id=instance_id, instance_name=instance_name, reason=str(exc),
                           response=_safe(exc.response), latency_ms=latency, purpose=purpose)
                if not exc.retryable:
                    return PlacedOrder(None, None, str(exc), latency)
                last_err = str(exc)
            except BrokerError as exc:
                last_err = str(exc)
                self._emit(sync, "API_ERROR", f"place_order failed: {exc}", Level.ERROR, trade_ref=trade_ref,
                           instance_id=instance_id, instance_name=instance_name, error=str(exc),
                           response=_safe(exc.response), attempt=attempt, purpose=purpose)
                if not exc.retryable:
                    return PlacedOrder(None, None, str(exc))
            except Exception as exc:  # unexpected adapter error
                last_err = repr(exc)
                self.bus.exception("API_ERROR", "place_order raised", exc, account_id=sync.account_id,
                                   trade_id=trade_ref)
            if attempt < attempts:
                # did the broker take it anyway?
                existing = await self._find_by_tag(adapter, sync, req.tag)
                if existing is not None:
                    rec_id = self._record(sync, existing.order_id, req, purpose, trade_db_id, trade_ref, instance_id,
                                          signal_price, None)
                    self._emit(sync, "ACCEPTED", f"order_id={existing.order_id} found in order book after error",
                               Level.WARNING, trade_ref=trade_ref, instance_id=instance_id, order_id=existing.order_id)
                    return PlacedOrder(existing.order_id, rec_id)
                delay = 0.5 * (2 ** (attempt - 1))
                self._emit(sync, "RETRY", f"attempt={attempt + 1} in {delay:.1f}s reason={last_err}", Level.WARNING,
                           trade_ref=trade_ref, instance_id=instance_id, attempt=attempt + 1, reason=last_err)
                await asyncio.sleep(delay)
        return PlacedOrder(None, None, last_err or "order failed")

    async def _find_by_tag(self, adapter: BrokerAdapter, sync: BrokerSync, tag: str | None) -> Order | None:
        if not tag:
            return None
        try:
            for o in await adapter.get_order_book():
                sync.handle_order(o, "poll")
                if o.tag == tag and o.status is not OrderStatus.REJECTED:
                    return o
        except Exception:
            return None
        return None

    def _record(self, sync: BrokerSync, order_id: str, req: OrderRequest, purpose: str, trade_db_id: int | None,
                trade_ref: str | None, instance_id: int | None, signal_price: float | None,
                latency: int | None) -> int:
        with self.db.session() as s:
            rec = s.scalar(select(OrderRecord).where(OrderRecord.account_id == sync.account_id,
                                                     OrderRecord.paper == sync.paper,
                                                     OrderRecord.broker_order_id == order_id))
            if rec is None:
                rec = OrderRecord(account_id=sync.account_id, paper=sync.paper, broker_order_id=order_id,
                                  exchange=req.instrument.exchange.value, tradingsymbol=req.instrument.tradingsymbol,
                                  token=req.instrument.token, side=req.side.value, order_type=req.order_type.value,
                                  product=req.product.value, quantity=req.quantity, status=OrderStatus.OPEN.value,
                                  placed_at=self.clock.now())
                s.add(rec)
            rec.purpose, rec.is_external = purpose, False
            rec.trade_id, rec.trade_ref, rec.instance_id = trade_db_id, trade_ref, instance_id
            rec.tag, rec.price, rec.trigger_price = req.tag, req.price, req.trigger_price
            rec.signal_price, rec.latency_ms = signal_price, latency
            s.flush()
            return rec.id

    async def wait_terminal(self, sync: BrokerSync, order_id: str, timeout: float) -> Order | None:
        return await sync.wait_for(order_id, lambda o: o.status.is_terminal, timeout)

    async def wait_working_or_terminal(self, sync: BrokerSync, order_id: str, timeout: float) -> Order | None:
        return await sync.wait_for(order_id, lambda o: o.status in (OrderStatus.TRIGGER_PENDING, OrderStatus.OPEN)
                                   or o.status.is_terminal, timeout)

    async def cancel(self, adapter: BrokerAdapter, sync: BrokerSync, order_id: str, *, trade_ref: str | None = None,
                     instance_id: int | None = None, reason: str = "", wait: float = 5.0) -> Order | None:
        current = sync.orders.get(order_id)
        if current is not None and current.status.is_terminal:
            return current
        self._emit(sync, "CANCEL_REQUEST", f"order_id={order_id} reason={reason}", trade_ref=trade_ref,
                   instance_id=instance_id, order_id=order_id, reason=reason)
        try:
            await adapter.cancel_order(order_id)
        except Exception as exc:
            self._emit(sync, "CANCEL_FAILED", f"order_id={order_id}: {exc}", Level.WARNING, trade_ref=trade_ref,
                       instance_id=instance_id, order_id=order_id, error=str(exc))
            await sync.poll_orders()
            return sync.orders.get(order_id)
        return await self.wait_terminal(sync, order_id, wait)

    async def modify(self, adapter: BrokerAdapter, sync: BrokerSync, req: OrderModify, *,
                     trade_ref: str | None = None) -> bool:
        self._emit(sync, "MODIFY_REQUEST", f"order_id={req.order_id} type={req.order_type.value} "
                   f"price={req.price} trigger={req.trigger_price} qty={req.quantity}", trade_ref=trade_ref,
                   order_id=req.order_id)
        try:
            await adapter.modify_order(req)
            return True
        except Exception as exc:
            self._emit(sync, "MODIFY_FAILED", f"order_id={req.order_id}: {exc}", Level.WARNING, trade_ref=trade_ref,
                       error=str(exc))
            return False


def _safe(resp: Any) -> Any:
    if resp is None:
        return None
    if isinstance(resp, (dict, list, str, int, float, bool)):
        return resp
    return repr(resp)
