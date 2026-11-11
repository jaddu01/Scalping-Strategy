"""JSON endpoints used by the browser (live state, toggles, kill switches, log queries/exports)."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import or_, select

from app.auth.access import account_for_control, instance_for_control, require_trader
from app.auth.deps import SessionInfo, audit, current_user, require_admin
from core.clock import combine
from storage.models import EventRecord, StrategyInstance, Trade

router = APIRouter(prefix="/api")


def _platform(request: Request) -> Any:
    p = request.app.state.platform
    if p is None:
        raise HTTPException(503, "trading engine is not running")
    return p


@router.get("/state")
async def state(request: Request, user: SessionInfo = Depends(current_user)) -> dict[str, Any]:
    return _platform(request).snapshot()


@router.get("/instances/{instance_id}/detail")
async def instance_detail(instance_id: int, request: Request, user: SessionInfo = Depends(current_user)) -> dict[str, Any]:
    p = _platform(request)
    return p.instance_detail(instance_id) or {"id": instance_id, "status": "STOPPED", "strategy": {}}


@router.post("/instances/{instance_id}/enable")
async def enable_instance(instance_id: int, request: Request, payload: dict[str, Any] = Body(default={}),
                          user: SessionInfo = Depends(require_trader)) -> dict[str, Any]:
    p = _platform(request)
    instance_for_control(request, user, instance_id)
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, instance_id)
        if row is None:
            raise HTTPException(404, "instance not found")
        if row.mode == "live" and not payload.get("confirm_live"):
            raise HTTPException(409, "live mode requires confirmation")
        if row.broker_account_id is None:
            raise HTTPException(400, "link a broker account first")
        name = row.name
    await p.strategies.enable(instance_id, user.username)
    audit(request, user, "instance_enabled", name)
    return {"ok": True, "status": "STARTING"}


@router.post("/instances/{instance_id}/disable")
async def disable_instance(instance_id: int, request: Request, payload: dict[str, Any] = Body(default={}),
                           user: SessionInfo = Depends(require_trader)) -> dict[str, Any]:
    p = _platform(request)
    instance_for_control(request, user, instance_id)
    choice = payload.get("on_open_position")
    open_pos = p.trades.open_trade_count(instance_id=instance_id) > 0
    if open_pos and choice not in ("exit_now", "finish_trade"):
        return JSONResponse({"needs_choice": True, "detail": "instance has an open position"}, status_code=409)  # type: ignore[return-value]
    status = await p.strategies.disable(instance_id, user.username, choice or "exit_now")
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, instance_id)
        name = row.name if row else str(instance_id)
    audit(request, user, "instance_disabled", name, {"on_open_position": choice, "open_position": open_pos})
    return {"ok": True, "status": status}


@router.post("/instances/{instance_id}/kill")
async def kill_instance(instance_id: int, request: Request, user: SessionInfo = Depends(require_trader)) -> dict[str, Any]:
    p = _platform(request)
    instance_for_control(request, user, instance_id)
    await p.strategies.kill(instance_id, user.username)
    audit(request, user, "kill_switch_instance", str(instance_id))
    return {"ok": True}


@router.post("/accounts/{account_id}/kill")
async def kill_account(account_id: int, request: Request, user: SessionInfo = Depends(require_trader)) -> dict[str, Any]:
    account_for_control(request, user, account_id)
    res = await _platform(request).kill_account(account_id, user.username)
    audit(request, user, "kill_switch_account", str(account_id), res)
    return {"ok": True, **res}


@router.post("/accounts/{account_id}/kill/reset")
async def reset_account_kill(account_id: int, request: Request, user: SessionInfo = Depends(require_trader)) -> dict[str, Any]:
    account_for_control(request, user, account_id)
    await _platform(request).reset_kill(user.username, account_id)
    audit(request, user, "kill_switch_reset_account", str(account_id))
    return {"ok": True}


@router.post("/kill/global")
async def kill_global(request: Request, user: SessionInfo = Depends(require_admin)) -> dict[str, Any]:
    res = await _platform(request).kill_global(user.username)
    audit(request, user, "kill_switch_global", "all", res)
    return {"ok": True, **res}


@router.post("/kill/reset")
async def reset_global_kill(request: Request, user: SessionInfo = Depends(require_admin)) -> dict[str, Any]:
    await _platform(request).reset_kill(user.username)
    audit(request, user, "kill_switch_reset_global", "all")
    return {"ok": True}


# ------------------------------------------------------------------ events
def _event_query(day_from: date | None, day_to: date | None, level: str | None, category: str | None,
                 event_type: str | None, instance_id: int | None, account_id: int | None, trade_id: str | None,
                 q: str | None) -> Any:
    stmt = select(EventRecord)
    if day_from:
        stmt = stmt.where(EventRecord.ts >= combine(day_from, time.min))
    if day_to:
        stmt = stmt.where(EventRecord.ts < combine(day_to + timedelta(days=1), time.min))
    if level:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if level in order:
            stmt = stmt.where(EventRecord.level.in_(order[order.index(level):]))
    if category:
        stmt = stmt.where(EventRecord.category == category)
    if event_type:
        stmt = stmt.where(EventRecord.event_type == event_type.upper())
    if instance_id:
        stmt = stmt.where(EventRecord.instance_id == instance_id)
    if account_id:
        stmt = stmt.where(EventRecord.account_id == account_id)
    if trade_id:
        stmt = stmt.where(EventRecord.trade_id == trade_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(EventRecord.message.ilike(like), EventRecord.event_type.ilike(like)))
    return stmt


def _event_dict(e: EventRecord) -> dict[str, Any]:
    from core.clock import fmt_hms_ms

    return {"id": e.id, "ts": e.ts.isoformat(timespec="milliseconds"), "time": fmt_hms_ms(e.ts), "level": e.level,
            "category": e.category, "event_type": e.event_type, "instance_id": e.instance_id,
            "instance_name": e.instance_name, "account_id": e.account_id, "account_label": e.account_label,
            "trade_id": e.trade_id, "message": e.message, "payload": e.payload}


@router.get("/events")
async def events(request: Request, user: SessionInfo = Depends(current_user), day_from: date | None = None,
                 day_to: date | None = None, level: str | None = None, category: str | None = None,
                 event_type: str | None = None, instance_id: int | None = None, account_id: int | None = None,
                 trade_id: str | None = None, q: str | None = None,
                 limit: int = Query(500, le=5000), before_id: int | None = None) -> dict[str, Any]:
    p = request.app.state.platform
    if p is not None and p.db_sink is not None:
        p.db_sink.flush()
    stmt = _event_query(day_from, day_to, level or None, category or None, event_type or None, instance_id,
                        account_id, trade_id or None, q or None)
    if before_id:
        stmt = stmt.where(EventRecord.id < before_id)
    with request.app.state.db.session() as s:
        rows = list(s.scalars(stmt.order_by(EventRecord.id.desc()).limit(limit)))
    return {"events": [_event_dict(e) for e in rows]}


@router.get("/events/export")
async def export_events(request: Request, user: SessionInfo = Depends(current_user), fmt: str = "csv",
                        day_from: date | None = None, day_to: date | None = None, level: str | None = None,
                        category: str | None = None, event_type: str | None = None, instance_id: int | None = None,
                        account_id: int | None = None, trade_id: str | None = None, q: str | None = None) -> Response:
    stmt = _event_query(day_from, day_to, level or None, category or None, event_type or None, instance_id,
                        account_id, trade_id or None, q or None).order_by(EventRecord.id)
    with request.app.state.db.session() as s:
        rows = [_event_dict(e) for e in s.scalars(stmt.limit(200000))]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "json":
        return Response(json.dumps(rows, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename=events_{stamp}.json"})
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["ts", "level", "category", "event_type", "instance_name", "account_label", "trade_id", "message", "payload"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] if c != "payload" else json.dumps(r[c], ensure_ascii=False) for c in cols])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=events_{stamp}.csv"})


def trade_timeline(db: Any, trade_ref: str) -> dict[str, Any]:
    """Full chain for one trade: reference candle → signal → entry → fill → SL → exit → P&L."""
    with db.session() as s:
        trade = s.scalar(select(Trade).where(Trade.trade_ref == trade_ref))
        if trade is None:
            return {}
        s.expunge(trade)
        evs = list(s.scalars(select(EventRecord).where(EventRecord.trade_id == trade_ref).order_by(EventRecord.id)))
        day_start = combine(trade.trade_date, time.min)
        signal_ts = trade.signal_time or (evs[0].ts if evs else None)
        pre: list[EventRecord] = []
        if trade.instance_id and signal_ts:
            cands = list(s.scalars(select(EventRecord).where(
                EventRecord.instance_id == trade.instance_id, EventRecord.ts >= day_start,
                EventRecord.ts <= signal_ts + timedelta(seconds=2),
                EventRecord.event_type.in_(["REF_CANDLE", "SIGNAL"])).order_by(EventRecord.id)))
            slot_no = (trade.meta or {}).get("slot_no")
            ref = [e for e in cands if e.event_type == "REF_CANDLE" and (e.payload or {}).get("slot") == slot_no]
            sig = [e for e in cands if e.event_type == "SIGNAL" and e.ts <= signal_ts + timedelta(seconds=2)
                   and e.trade_id is None]
            pre = ref[-1:] + sig[-1:]
    chain = sorted(pre, key=lambda e: e.ts) + evs
    out = []
    prev_ts = None
    for e in chain:
        d = _event_dict(e)
        d["latency_ms"] = int((e.ts - prev_ts).total_seconds() * 1000) if prev_ts else None
        prev_ts = e.ts
        out.append(d)
    t = {c.name: getattr(trade, c.name) for c in Trade.__table__.columns}
    return {"trade": t, "events": out}


@router.get("/trades/{trade_ref}/timeline")
async def timeline(trade_ref: str, request: Request, user: SessionInfo = Depends(current_user)) -> dict[str, Any]:
    p = request.app.state.platform
    if p is not None and p.db_sink is not None:
        p.db_sink.flush()
    data = trade_timeline(request.app.state.db, trade_ref)
    if not data:
        raise HTTPException(404, "trade not found")
    return json.loads(json.dumps(data, default=str))
