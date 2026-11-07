"""Trade & order history (filterable, CSV export, daily summary) and the trade timeline view."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from app.api.routes import trade_timeline
from app.auth.deps import SessionInfo, current_user
from app.web.render import render
from storage.models import BrokerAccount, OrderRecord, StrategyInstance, Trade

router = APIRouter()


def _filters(request: Request) -> dict[str, Any]:
    q = request.query_params
    today = date.today()

    def d(name: str, default: date) -> date:
        try:
            return date.fromisoformat(q.get(name) or "")
        except ValueError:
            return default

    return {"day_from": d("day_from", today - timedelta(days=7)), "day_to": d("day_to", today),
            "instance_id": int(q["instance_id"]) if q.get("instance_id", "").isdigit() else None,
            "account_id": int(q["account_id"]) if q.get("account_id", "").isdigit() else None,
            "symbol": (q.get("symbol") or "").strip().upper(), "status": q.get("status") or "",
            "mode": q.get("mode") or ""}


def _trades(request: Request, f: dict[str, Any]) -> list[Trade]:
    stmt = select(Trade).where(Trade.trade_date >= f["day_from"], Trade.trade_date <= f["day_to"])
    if f["instance_id"]:
        stmt = stmt.where(Trade.instance_id == f["instance_id"])
    if f["account_id"]:
        stmt = stmt.where(Trade.account_id == f["account_id"])
    if f["symbol"]:
        stmt = stmt.where(Trade.tradingsymbol.contains(f["symbol"]))
    if f["status"]:
        stmt = stmt.where(Trade.status == f["status"])
    if f["mode"]:
        stmt = stmt.where(Trade.mode == f["mode"])
    with request.app.state.db.session() as s:
        rows = list(s.scalars(stmt.order_by(Trade.id.desc()).limit(5000)))
        for r in rows:
            s.expunge(r)
    return rows


def _lookups(request: Request) -> tuple[dict[int, str], dict[int, str]]:
    with request.app.state.db.session() as s:
        inst = {i.id: i.name for i in s.scalars(select(StrategyInstance))}
        acc = {a.id: a.label for a in s.scalars(select(BrokerAccount))}
    return inst, acc


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    f = _filters(request)
    rows = _trades(request, f)
    inst, acc = _lookups(request)
    summary: dict[tuple[date, str], dict[str, float]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "gross": 0.0, "charges": 0.0, "net": 0.0})
    for t in rows:
        if t.status != "CLOSED":
            continue
        name = inst.get(t.instance_id or 0, (t.meta or {}).get("deleted_instance", "–"))
        b = summary[(t.trade_date, name)]
        b["trades"] += 1
        b["wins"] += 1 if (t.gross_pnl or 0) > 0 else 0
        b["losses"] += 1 if (t.gross_pnl or 0) < 0 else 0
        b["gross"] += t.gross_pnl or 0
        b["charges"] += t.charges or 0
        b["net"] += t.net_pnl or 0
    summ = sorted(summary.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True)
    totals = {"gross": sum(b["gross"] for _, b in summ), "net": sum(b["net"] for _, b in summ),
              "charges": sum(b["charges"] for _, b in summ), "trades": sum(b["trades"] for _, b in summ)}
    return render(request, "trades.html", {"rows": rows, "f": f, "inst": inst, "acc": acc, "summary": summ,
                                           "totals": totals, "qs": request.url.query})


@router.get("/trades.csv")
async def trades_csv(request: Request, user: SessionInfo = Depends(current_user)) -> Response:
    f = _filters(request)
    rows = _trades(request, f)
    inst, acc = _lookups(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["trade_ref", "trade_date", "instance", "account", "mode", "slot_id", "direction", "tradingsymbol", "strike",
            "expiry", "lots", "quantity", "signal_time", "signal_spot", "signal_trigger", "entry_time", "entry_price",
            "spot_sl", "exit_at", "exit_time", "exit_price", "exit_reason", "gross_pnl", "charges", "net_pnl", "status"]
    w.writerow(cols)
    for t in rows:
        vals = {c: getattr(t, c, "") for c in cols if hasattr(t, c)}
        vals["instance"] = inst.get(t.instance_id or 0, "")
        vals["account"] = acc.get(t.account_id or 0, "")
        w.writerow([vals.get(c, "") if vals.get(c) is not None else "" for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=trades_{f['day_from']}_{f['day_to']}.csv"})


@router.get("/trades/{trade_ref}", response_class=HTMLResponse)
async def trade_view(trade_ref: str, request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    if p is not None and p.db_sink is not None:
        p.db_sink.flush()
    data = trade_timeline(request.app.state.db, trade_ref)
    if not data:
        raise HTTPException(404, "trade not found")
    inst, acc = _lookups(request)
    with request.app.state.db.session() as s:
        orders = list(s.scalars(select(OrderRecord).where(OrderRecord.trade_ref == trade_ref).order_by(OrderRecord.id)))
    return render(request, "timeline.html", {"t": data["trade"], "events": data["events"], "orders": orders,
                                             "inst": inst, "acc": acc})


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    f = _filters(request)
    from core.clock import combine
    from datetime import time

    stmt = select(OrderRecord).where(OrderRecord.updated_at >= combine(f["day_from"], time.min),
                                     OrderRecord.updated_at < combine(f["day_to"] + timedelta(days=1), time.min))
    if f["account_id"]:
        stmt = stmt.where(OrderRecord.account_id == f["account_id"])
    if f["instance_id"]:
        stmt = stmt.where(OrderRecord.instance_id == f["instance_id"])
    if f["symbol"]:
        stmt = stmt.where(OrderRecord.tradingsymbol.contains(f["symbol"]))
    if request.query_params.get("external") == "1":
        stmt = stmt.where(OrderRecord.is_external.is_(True))
    with request.app.state.db.session() as s:
        rows = list(s.scalars(stmt.order_by(OrderRecord.id.desc()).limit(2000)))
    inst, acc = _lookups(request)
    return render(request, "orders.html", {"rows": rows, "f": f, "inst": inst, "acc": acc,
                                           "external": request.query_params.get("external") == "1"})
