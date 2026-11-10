"""Strategy instances: list, create, clone, edit (schema-driven form), delete, detail view."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.deps import SessionInfo, audit, current_user, require_admin
from app.web.forms import build_fields, errors_by_field, parse_form
from app.web.render import redirect, render
from core.settings_schema import InstanceRiskLimits
from storage.models import BrokerAccount, OrderRecord, StrategyInstance, Trade
from storage.repo import diff_dicts
from strategies.registry import all_templates, get_template

router = APIRouter()


def _accounts(request: Request) -> list[dict[str, Any]]:
    p = request.app.state.platform
    with request.app.state.db.session() as s:
        rows = list(s.scalars(select(BrokerAccount).order_by(BrokerAccount.label)))
    out = []
    for a in rows:
        rt = p.accounts.get(a.id) if p else None
        out.append({"id": a.id, "label": a.label, "broker_type": a.broker_type,
                    "status": rt.status if rt else a.status, "is_paper": a.broker_type == "paper"})
    return out


def _form_ctx(request: Request, template_id: str, values: dict[str, Any], errors: dict[str, str] | None = None,
              general: list[str] | None = None, instance: StrategyInstance | None = None) -> dict[str, Any]:
    cls = get_template(template_id)
    errors = errors or {}
    return {
        "templates_list": [(k, v.display_name) for k, v in all_templates().items() if not k.startswith("test_")],
        "template_id": template_id,
        "template": cls,
        "param_fields": build_fields(cls.settings_model, values.get("params"), errors, "params."),
        "risk_fields": build_fields(InstanceRiskLimits, values.get("risk_limits"), errors, "risk."),
        "values": values,
        "errors": errors,
        "general_errors": general or [],
        "accounts": _accounts(request),
        "instance": instance,
    }


@router.get("/strategies", response_class=HTMLResponse)
async def list_instances(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    with request.app.state.db.session() as s:
        rows = list(s.scalars(select(StrategyInstance).order_by(StrategyInstance.name)))
        accounts = {a.id: a.label for a in s.scalars(select(BrokerAccount))}
    items = []
    for r in rows:
        rt = p.strategies.runtimes.get(r.id) if p else None
        items.append({"row": r, "account": accounts.get(r.broker_account_id or 0, "–"),
                      "status": rt.status if rt else r.status,
                      "open": bool(p and p.trades.open_trade_count(instance_id=r.id))})
    return render(request, "strategies.html", {"items": items,
                                               "templates_list": [(k, v.display_name) for k, v in all_templates().items()
                                                                  if not k.startswith("test_")]})


@router.get("/strategies/new", response_class=HTMLResponse)
async def new_instance(request: Request, template: str = "nifty_ref_breakout",
                       user: SessionInfo = Depends(require_admin)) -> HTMLResponse:
    cls = get_template(template)
    values = {"name": "", "mode": "paper", "notes": "", "broker_account_id": None,
              "params": cls.settings_model().model_dump(), "risk_limits": InstanceRiskLimits().model_dump()}
    return render(request, "strategy_form.html", _form_ctx(request, template, values))


async def _parse_instance_form(request: Request, template_id: str) -> tuple[dict[str, Any], dict[str, str], list[str],
                                                                               Any, Any]:
    form = await request.form()
    cls = get_template(template_id)
    values: dict[str, Any] = {
        "name": (form.get("name") or "").strip(),
        "mode": form.get("mode") or "paper",
        "notes": form.get("notes") or "",
        "broker_account_id": int(form.get("broker_account_id")) if form.get("broker_account_id") else None,
    }
    raw_params = parse_form(cls.settings_model, form, "params.")
    raw_risk = parse_form(InstanceRiskLimits, form, "risk.")
    values["params"], values["risk_limits"] = raw_params, raw_risk
    errors: dict[str, str] = {}
    general: list[str] = []
    params = risk = None
    try:
        params = cls.settings_model.model_validate(raw_params)
    except ValidationError as exc:
        e, g = errors_by_field(exc, "params.")
        errors.update(e)
        general += g
    try:
        risk = InstanceRiskLimits.model_validate(raw_risk)
    except ValidationError as exc:
        e, g = errors_by_field(exc, "risk.")
        errors.update(e)
        general += g
    if not values["name"]:
        errors["name"] = "required"
    if values["mode"] not in ("paper", "live"):
        errors["mode"] = "must be paper or live"
    if values["mode"] == "live":
        if form.get("confirm_live") != "yes":
            errors["mode"] = "confirm that this instance will place REAL orders"
        with request.app.state.db.session() as s:
            acc = s.get(BrokerAccount, values["broker_account_id"]) if values["broker_account_id"] else None
            if acc is None:
                errors["broker_account_id"] = "live mode needs a broker account"
            elif acc.broker_type == "paper":
                errors["broker_account_id"] = "live mode needs a real (non-paper) broker account"
    return values, errors, general, params, risk


@router.post("/strategies/new")
async def create_instance(request: Request, template: str = "nifty_ref_breakout",
                          user: SessionInfo = Depends(require_admin)) -> Response:
    values, errors, general, params, risk = await _parse_instance_form(request, template)
    with request.app.state.db.session() as s:
        if values["name"] and s.scalar(select(StrategyInstance).where(StrategyInstance.name == values["name"])):
            errors["name"] = "an instance with this name already exists"
    if errors or general:
        return render(request, "strategy_form.html", _form_ctx(request, template, values, errors, general), 400)
    with request.app.state.db.session() as s:
        row = StrategyInstance(name=values["name"], template_id=template, params=params.model_dump(mode="json"),
                               broker_account_id=values["broker_account_id"], mode=values["mode"], enabled=False,
                               risk_limits=risk.model_dump(mode="json"), notes=values["notes"])
        s.add(row)
        s.flush()
        iid = row.id
    audit(request, user, "instance_created", values["name"], {"template": template, "mode": values["mode"],
                                                               "params": params.model_dump(mode="json")})
    return redirect(f"/strategies/{iid}", ok="Instance created (disabled). Enable it from the dashboard or here.")


@router.get("/strategies/{iid}/edit", response_class=HTMLResponse)
async def edit_instance(iid: int, request: Request, user: SessionInfo = Depends(require_admin)) -> HTMLResponse:
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        s.expunge(row)
    values = {"name": row.name, "mode": row.mode, "notes": row.notes, "broker_account_id": row.broker_account_id,
              "params": get_template(row.template_id).settings_model.model_validate(row.params or {}).model_dump(),
              "risk_limits": InstanceRiskLimits.model_validate(row.risk_limits or {}).model_dump()}
    return render(request, "strategy_form.html", _form_ctx(request, row.template_id, values, instance=row))


@router.post("/strategies/{iid}/edit")
async def save_instance(iid: int, request: Request, user: SessionInfo = Depends(require_admin)) -> Response:
    p = request.app.state.platform
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        s.expunge(row)
    values, errors, general, params, risk = await _parse_instance_form(request, row.template_id)
    with request.app.state.db.session() as s:
        dup = s.scalar(select(StrategyInstance).where(StrategyInstance.name == values["name"], StrategyInstance.id != iid))
        if dup:
            errors["name"] = "an instance with this name already exists"
    open_pos = bool(p and p.trades.open_trade_count(instance_id=iid))
    structural = (values["mode"] != row.mode or values["broker_account_id"] != row.broker_account_id)
    if structural and open_pos:
        general.append("cannot change mode or broker account while the instance has an open position")
    if errors or general:
        return render(request, "strategy_form.html",
                      _form_ctx(request, row.template_id, values, errors, general, instance=row), 400)
    new_params = params.model_dump(mode="json")
    changes = diff_dicts({"name": row.name, "mode": row.mode, "broker_account_id": row.broker_account_id,
                          "params": row.params, "risk_limits": row.risk_limits, "notes": row.notes},
                         {"name": values["name"], "mode": values["mode"], "broker_account_id": values["broker_account_id"],
                          "params": new_params, "risk_limits": risk.model_dump(mode="json"), "notes": values["notes"]})
    with request.app.state.db.session() as s:
        r = s.get(StrategyInstance, iid)
        r.name, r.mode, r.broker_account_id, r.notes = values["name"], values["mode"], values["broker_account_id"], values["notes"]
        r.params, r.risk_limits = new_params, risk.model_dump(mode="json")
        enabled = r.enabled
    audit(request, user, "instance_updated", values["name"], changes)
    msg = "Saved."
    if p is not None and enabled and iid in p.strategies.runtimes:
        if structural or values["name"] != row.name:
            await p.strategies.start_instance(iid)
            msg = "Saved and restarted the instance."
        else:
            applied = await p.strategies.update_params(iid, new_params)
            p.strategies.reload_limits(iid)
            msg = "Saved and applied." if applied else "Saved. New parameters apply from the next trade slot."
    return redirect(f"/strategies/{iid}", ok=msg)


@router.post("/strategies/{iid}/clone")
async def clone_instance(iid: int, request: Request, user: SessionInfo = Depends(require_admin)) -> Response:
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        base = f"{row.name} (copy)"
        name, n = base, 2
        while s.scalar(select(StrategyInstance).where(StrategyInstance.name == name)):
            name, n = f"{base} {n}", n + 1
        new = StrategyInstance(name=name, template_id=row.template_id, params=dict(row.params),
                               broker_account_id=row.broker_account_id, mode="paper", enabled=False,
                               risk_limits=dict(row.risk_limits), notes=row.notes)
        s.add(new)
        s.flush()
        nid = new.id
    audit(request, user, "instance_cloned", name, {"from": iid})
    return redirect(f"/strategies/{nid}/edit", ok="Cloned in paper mode — review and save")


@router.post("/strategies/{iid}/delete")
async def delete_instance(iid: int, request: Request, user: SessionInfo = Depends(require_admin)) -> Response:
    p = request.app.state.platform
    if p is not None and (iid in p.strategies.runtimes or p.trades.open_trade_count(instance_id=iid)):
        return redirect(f"/strategies/{iid}", err="Disable the instance (and close its position) before deleting it")
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        name = row.name
        for t in s.scalars(select(Trade).where(Trade.instance_id == iid)):
            t.instance_id = None
            t.meta = {**(t.meta or {}), "deleted_instance": name}
        for o in s.scalars(select(OrderRecord).where(OrderRecord.instance_id == iid)):
            o.instance_id = None
        s.delete(row)
    audit(request, user, "instance_deleted", name)
    return redirect("/strategies", ok=f"Deleted {name} (its trade history is kept)")


@router.get("/strategies/{iid}", response_class=HTMLResponse)
async def instance_detail(iid: int, request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        s.expunge(row)
        acc = s.get(BrokerAccount, row.broker_account_id) if row.broker_account_id else None
        today = (p.clock.now() if p else None)
        day = today.date() if today else None
        trades = list(s.scalars(select(Trade).where(Trade.instance_id == iid).order_by(Trade.id.desc()).limit(50)))
        orders = list(s.scalars(select(OrderRecord).where(OrderRecord.instance_id == iid)
                                .order_by(OrderRecord.id.desc()).limit(50)))
    cls = get_template(row.template_id)
    params = cls.settings_model.model_validate(row.params or {})
    rt = p.strategies.runtimes.get(iid) if p else None
    return render(request, "strategy_detail.html", {
        "inst": row, "account": acc, "template": cls, "params": params.model_dump(mode="json"),
        "risk": InstanceRiskLimits.model_validate(row.risk_limits or {}).model_dump(mode="json"),
        "trades": trades, "orders": orders, "rt": rt.snapshot() if rt else None, "day": day,
        "open_position": bool(p and p.trades.open_trade_count(instance_id=iid)),
    })


@router.get("/strategies/{iid}/slots", response_class=HTMLResponse)
async def instance_slots(iid: int, request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    """HTMX partial: today's slots, refreshed every few seconds."""
    p = request.app.state.platform
    rt = p.strategies.runtimes.get(iid) if p else None
    snap = rt.snapshot() if rt else None
    views: dict[str, dict[str, Any]] = {}
    totals = {"points": 0.0, "pnl": 0.0, "trades": 0}
    st = (snap or {}).get("strategy") or {}
    refs = [s["trade_id"] for s in (st.get("slots") or []) + (st.get("signals") or []) if s.get("trade_id")]
    if refs:
        from app.web.slot_view import slot_trade_view

        with request.app.state.db.session() as s:
            rows = {t.trade_ref: t for t in s.scalars(select(Trade).where(Trade.trade_ref.in_(refs)))}
            data = {ref: {"direction": t.direction, "signal_spot": t.signal_spot, "quantity": t.quantity,
                          "lot_size": t.lot_size, "entry_price": t.entry_price, "exit_price": t.exit_price,
                          "exit_trigger_spot": t.exit_trigger_spot, "gross_pnl": t.gross_pnl, "net_pnl": t.net_pnl,
                          "status": t.status, "exit_time": t.exit_time, "partials": (t.meta or {}).get("partials")}
                    for ref, t in rows.items()}
        live_by_ref = {lt.ref: lt for lt in list(p.trades.live.values())} if p else {}
        for ref, tr in data.items():
            lt = live_by_ref.get(ref)
            live = None
            if lt is not None:
                live = {"spot": p.market.ltp(lt.spot_key) if lt.spot_key else None,
                        "premium": p.market.ltp(lt.option_series), "open_qty": lt.open_qty}
                tr = {**tr, "partials": lt.partials, "status": lt.status}
            v = slot_trade_view(tr, live)
            views[ref] = v
            if v.get("points") is not None:
                totals["points"] += v["points"]
            if v.get("pnl") is not None:
                totals["pnl"] += v["pnl"]
            totals["trades"] += 1
    totals = {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}
    return render(request, "partials/slots.html", {"rt": snap, "views": views, "totals": totals})

