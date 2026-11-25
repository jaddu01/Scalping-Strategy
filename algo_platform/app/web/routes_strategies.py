"""Strategy instances: list, create, clone, edit (schema-driven form), delete, detail view."""
from __future__ import annotations

from datetime import date as date_cls  # `date` is a query parameter name below
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.access import (require_visible, can_control, instance_for_control, is_admin, owner_choices, owner_filter,
                             require_trader, resolve_owner, usernames)
from app.auth.deps import SessionInfo, audit, current_user
from app.web.forms import build_fields, errors_by_field, parse_form
from app.web.render import redirect, render
from core.settings_schema import InstanceRiskLimits
from app.web.shared import lots_of, merged, personal_keys, split_fields
from storage.models import BrokerAccount, OrderRecord, SharedStrategy, StrategyInstance, Trade
from storage.repo import diff_dicts
from strategies.registry import all_templates, get_template

router = APIRouter()


def _accounts(request: Request, user: SessionInfo) -> list[dict[str, Any]]:
    """Accounts a strategy may be linked to: a partner sees only their own; an admin sees all (with owner),
    and the save is still refused unless the account's owner is the strategy's owner."""
    p = request.app.state.platform
    names = usernames(request.app.state.db)
    with request.app.state.db.session() as s:
        stmt = select(BrokerAccount).order_by(BrokerAccount.label)
        if not is_admin(user):
            stmt = stmt.where(BrokerAccount.owner_user_id == user.user_id)
        rows = list(s.scalars(stmt))
    out = []
    for a in rows:
        rt = p.accounts.get(a.id) if p else None
        out.append({"id": a.id, "label": a.label, "broker_type": a.broker_type,
                    "status": rt.status if rt else a.status, "is_paper": a.broker_type == "paper",
                    "owner": names.get(a.owner_user_id or 0, "–") if is_admin(user) else None})
    return out


def _form_ctx(request: Request, user: SessionInfo, template_id: str, values: dict[str, Any],
              errors: dict[str, str] | None = None, general: list[str] | None = None,
              instance: StrategyInstance | None = None, kind: str = "new",
              shared: SharedStrategy | None = None) -> dict[str, Any]:
    """kind: new   = a new shared strategy + your run of it (all fields)
             run   = your run of a shared strategy: account, mode, lots, risk (the rules are read-only)
             rules = the shared rules only (no account / mode / lots / risk)
             legacy = a stand-alone instance with its own rules (all fields)"""
    cls = get_template(template_id)
    errors = errors or {}
    fields = build_fields(cls.settings_model, values.get("params"), errors, "params.")
    if kind == "run":
        fields = split_fields(fields, personal=True)
    elif kind == "rules":
        fields = split_fields(fields, personal=False)
    if kind == "rules":
        action = f"/strategies/shared/{shared.id}/edit"  # type: ignore[union-attr]
        cancel = f"/strategies/shared/{shared.id}"  # type: ignore[union-attr]
    elif instance is not None:
        action, cancel = f"/strategies/{instance.id}/edit", f"/strategies/{instance.id}"
    elif shared is not None:
        action, cancel = f"/strategies/shared/{shared.id}/run", f"/strategies/shared/{shared.id}"
    else:
        action, cancel = f"/strategies/new?template={template_id}", "/strategies"
    rules_view = None
    if kind == "run" and shared is not None:
        own = set(personal_keys(cls.settings_model))
        # the shared rules shown read-only: from the rules alone, so a run's invalid own values (being corrected
        # on this very form) can never break the page
        try:
            shown = cls.settings_model.model_validate(merged(cls.settings_model, shared.params, {})).model_dump(
                mode="json")
        except ValidationError:
            shown = dict(shared.params or {})
        rules_view = {k: v for k, v in shown.items() if k not in own}
    return {
        "kind": kind, "shared": shared, "action": action, "cancel": cancel, "rules_view": rules_view,
        "templates_list": [(k, v.display_name) for k, v in all_templates().items() if not k.startswith("test_")],
        "template_id": template_id,
        "template": cls,
        "param_fields": fields,
        "risk_fields": build_fields(InstanceRiskLimits, values.get("risk_limits"), errors, "risk."),
        "values": values,
        "errors": errors,
        "general_errors": general or [],
        "accounts": _accounts(request, user),
        "owners": owner_choices(request.app.state.db) if is_admin(user) else [],
        "instance": instance,
    }


@router.get("/strategies", response_class=HTMLResponse)
async def list_instances(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    owner_val, owner_id = owner_filter(request, user)
    names = usernames(request.app.state.db)
    with request.app.state.db.session() as s:
        stmt = select(StrategyInstance).order_by(StrategyInstance.name)
        if owner_id is not None:
            stmt = stmt.where(StrategyInstance.owner_user_id == owner_id)
        rows = list(s.scalars(stmt))
        accounts = {a.id: a.label for a in s.scalars(select(BrokerAccount))}
        shared = list(s.scalars(select(SharedStrategy).order_by(SharedStrategy.name)))
        for sh in shared:
            s.expunge(sh)
    items = []
    for r in rows:
        rt = p.strategies.runtimes.get(r.id) if p else None
        items.append({"row": r, "account": accounts.get(r.broker_account_id or 0, "–"),
                      "status": rt.status if rt else r.status,
                      "open": bool(p and p.trades.open_trade_count(instance_id=r.id)),
                      "lots": lots_of(r.params),
                      "owner": names.get(r.owner_user_id or 0, "–"), "can_edit": can_control(user, r.owner_user_id)})
    by_strategy: dict[int, list[dict[str, Any]]] = {}
    for it in items:
        if it["row"].strategy_id is not None:
            by_strategy.setdefault(it["row"].strategy_id, []).append(it)
    groups = [{"s": sh, "runs": by_strategy.get(sh.id, []), "rules_by": names.get(sh.created_by or 0, "admin"),
               "mine": any(it["row"].owner_user_id == user.user_id for it in by_strategy.get(sh.id, [])),
               "can_edit_rules": can_control(user, sh.created_by)} for sh in shared
              # someone else's rules with none of the viewer's runs are not listed (private to that partner)
              if owner_id is None or by_strategy.get(sh.id) or sh.created_by == owner_id]
    known = {sh.id for sh in shared}
    standalone = [it for it in items if it["row"].strategy_id not in known]
    return render(request, "strategies.html", {"groups": groups, "standalone": standalone, "owner": owner_val,
                                               "owners": owner_choices(request.app.state.db),
                                               "templates_list": [(k, v.display_name) for k, v in all_templates().items()
                                                                  if not k.startswith("test_")]})


@router.get("/strategies/new", response_class=HTMLResponse)
async def new_instance(request: Request, template: str = "nifty_ref_breakout",
                       user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    cls = get_template(template)
    values = {"name": "", "mode": "paper", "notes": "", "broker_account_id": None, "owner_user_id": user.user_id,
              "params": cls.settings_model().model_dump(), "risk_limits": InstanceRiskLimits().model_dump()}
    return render(request, "strategy_form.html", _form_ctx(request, user, template, values))


async def _parse_instance_form(request: Request, template_id: str, user: SessionInfo,
                               current_owner: int | None = None, rules: dict[str, Any] | None = None
                               ) -> tuple[dict[str, Any], dict[str, str], list[str], Any, Any]:
    """With `rules` (a run of a shared strategy) only the personal params come from the form."""
    form = await request.form()
    cls = get_template(template_id)
    values: dict[str, Any] = {
        "name": (form.get("name") or "").strip(),
        "mode": form.get("mode") or "paper",
        "notes": form.get("notes") or "",
        "broker_account_id": int(form.get("broker_account_id")) if form.get("broker_account_id") else None,
        "owner_user_id": resolve_owner(form.get("owner_user_id"), user, request.app.state.db, current_owner),
    }
    raw_params = parse_form(cls.settings_model, form, "params.")
    if rules is not None:
        own = {k: raw_params[k] for k in personal_keys(cls.settings_model) if k in raw_params}
        raw_params = merged(cls.settings_model, rules, own)
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
    if values["broker_account_id"]:
        with request.app.state.db.session() as s:
            acc = s.get(BrokerAccount, values["broker_account_id"])
            if acc is None:
                errors["broker_account_id"] = "account not found"
            elif acc.owner_user_id != values["owner_user_id"]:
                # each strategy trades only on its owner's own broker account
                errors["broker_account_id"] = "this broker account belongs to another partner"
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
                          user: SessionInfo = Depends(require_trader)) -> Response:
    """A new shared strategy (the rules, for everyone) plus the creator's own run of it."""
    values, errors, general, params, risk = await _parse_instance_form(request, template, user)
    with request.app.state.db.session() as s:
        if values["name"] and (s.scalar(select(StrategyInstance).where(StrategyInstance.name == values["name"]))
                               or s.scalar(select(SharedStrategy).where(SharedStrategy.name == values["name"]))):
            errors["name"] = "a strategy with this name already exists"
    if errors or general:
        return render(request, "strategy_form.html", _form_ctx(request, user, template, values, errors, general), 400)
    with request.app.state.db.session() as s:
        shared = SharedStrategy(name=values["name"], template_id=template, params=params.model_dump(mode="json"),
                                notes=values["notes"], created_by=user.user_id)
        s.add(shared)
        s.flush()
        row = StrategyInstance(name=values["name"], template_id=template, params=params.model_dump(mode="json"),
                               broker_account_id=values["broker_account_id"], mode=values["mode"], enabled=False,
                               risk_limits=risk.model_dump(mode="json"), notes=values["notes"],
                               owner_user_id=values["owner_user_id"], strategy_id=shared.id)
        s.add(row)
        s.flush()
        iid = row.id
    audit(request, user, "instance_created", values["name"], {"template": template, "mode": values["mode"],
                                                               "owner_user_id": values["owner_user_id"],
                                                               "params": params.model_dump(mode="json")})
    return redirect(f"/strategies/{iid}", ok="Instance created (disabled). Enable it from the dashboard or here.")


@router.get("/strategies/{iid}/edit", response_class=HTMLResponse)
async def edit_instance(iid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    row = instance_for_control(request, user, iid)
    shared = _shared_of(request, row)
    values = {"name": row.name, "mode": row.mode, "notes": row.notes, "broker_account_id": row.broker_account_id,
              "owner_user_id": row.owner_user_id,
              "params": get_template(row.template_id).settings_model.model_validate(row.params or {}).model_dump(),
              "risk_limits": InstanceRiskLimits.model_validate(row.risk_limits or {}).model_dump()}
    return render(request, "strategy_form.html", _form_ctx(request, user, row.template_id, values, instance=row,
                                                           kind="run" if shared else "legacy", shared=shared))


def _shared_of(request: Request, row: StrategyInstance) -> SharedStrategy | None:
    if row.strategy_id is None:
        return None
    with request.app.state.db.session() as s:
        sh = s.get(SharedStrategy, row.strategy_id)
        if sh is not None:
            s.expunge(sh)
        return sh


@router.post("/strategies/{iid}/edit")
async def save_instance(iid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    p = request.app.state.platform
    row = instance_for_control(request, user, iid)
    shared = _shared_of(request, row)
    kind = "run" if shared else "legacy"
    values, errors, general, params, risk = await _parse_instance_form(request, row.template_id, user, row.owner_user_id,
                                                                       rules=shared.params if shared else None)
    with request.app.state.db.session() as s:
        dup = s.scalar(select(StrategyInstance).where(StrategyInstance.name == values["name"], StrategyInstance.id != iid))
        if dup:
            errors["name"] = "an instance with this name already exists"
    open_pos = bool(p and p.trades.open_trade_count(instance_id=iid))
    structural = (values["mode"] != row.mode or values["broker_account_id"] != row.broker_account_id
                  or values["owner_user_id"] != row.owner_user_id)
    if structural and open_pos:
        general.append("cannot change mode, broker account or owner while the instance has an open position")
    if errors or general:
        return render(request, "strategy_form.html",
                      _form_ctx(request, user, row.template_id, values, errors, general, instance=row,
                                kind=kind, shared=shared), 400)
    new_params = params.model_dump(mode="json")
    changes = diff_dicts({"name": row.name, "mode": row.mode, "broker_account_id": row.broker_account_id,
                          "owner_user_id": row.owner_user_id,
                          "params": row.params, "risk_limits": row.risk_limits, "notes": row.notes},
                         {"name": values["name"], "mode": values["mode"], "broker_account_id": values["broker_account_id"],
                          "owner_user_id": values["owner_user_id"],
                          "params": new_params, "risk_limits": risk.model_dump(mode="json"), "notes": values["notes"]})
    with request.app.state.db.session() as s:
        r = s.get(StrategyInstance, iid)
        r.name, r.mode, r.broker_account_id, r.notes = values["name"], values["mode"], values["broker_account_id"], values["notes"]
        r.params, r.risk_limits = new_params, risk.model_dump(mode="json")
        r.owner_user_id = values["owner_user_id"]
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
async def clone_instance(iid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    """Any partner may copy any strategy (the partners share them). For a shared strategy that means
    starting their own run of it; a stand-alone instance is copied. The copy belongs to whoever made it
    and keeps the broker account only if that account is theirs too."""
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        if row.strategy_id is not None:
            return redirect(f"/strategies/shared/{row.strategy_id}/run")
        acc = s.get(BrokerAccount, row.broker_account_id) if row.broker_account_id else None
        keep_acc = row.broker_account_id if acc is not None and acc.owner_user_id == user.user_id else None
        base = f"{row.name} (copy)"
        name, n = base, 2
        while s.scalar(select(StrategyInstance).where(StrategyInstance.name == name)):
            name, n = f"{base} {n}", n + 1
        new = StrategyInstance(name=name, template_id=row.template_id, params=dict(row.params),
                               broker_account_id=keep_acc, mode="paper", enabled=False,
                               risk_limits=dict(row.risk_limits), notes=row.notes, owner_user_id=user.user_id)
        s.add(new)
        s.flush()
        nid = new.id
    audit(request, user, "instance_cloned", name, {"from": iid})
    msg = "Cloned in paper mode — review and save"
    if row.broker_account_id and keep_acc is None:
        msg += " (pick one of your own broker accounts)"
    return redirect(f"/strategies/{nid}/edit", ok=msg)


@router.post("/strategies/{iid}/delete")
async def delete_instance(iid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    instance_for_control(request, user, iid)
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
async def instance_detail(iid: int, request: Request, user: SessionInfo = Depends(current_user),
                          date: str | None = None) -> HTMLResponse:
    """A run's page. `?date=YYYY-MM-DD` (a past day, e.g. picked on the dashboard) shows that day's trades and
    orders instead of today's live slots."""
    p = request.app.state.platform
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        require_visible(user, row.owner_user_id)
        s.expunge(row)
        acc = s.get(BrokerAccount, row.broker_account_id) if row.broker_account_id else None
        today = (p.clock.now() if p else None)
        day = today.date() if today else None
        view_day = _past_day(date, day)
        if view_day is not None:
            trades = list(s.scalars(select(Trade).where(Trade.instance_id == iid, Trade.trade_date == view_day)
                                    .order_by(Trade.id)))
            ids = [t.id for t in trades]
            orders = list(s.scalars(select(OrderRecord).where(OrderRecord.instance_id == iid,
                                                              OrderRecord.trade_id.in_(ids))
                                    .order_by(OrderRecord.id))) if ids else []
        else:
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
        "view_day": view_day, "day_summary": _day_summary(trades) if view_day else None,
        "prev_day": _weekday(view_day or day, -1) if day else None,
        "next_day": _weekday(view_day, 1) if view_day else None,
        "open_position": bool(p and p.trades.open_trade_count(instance_id=iid)),
        "owner": usernames(request.app.state.db).get(row.owner_user_id or 0, "–"),
        "can_edit": can_control(user, row.owner_user_id),
        "shared": _shared_of(request, row),
    })


def _past_day(text: str | None, today: Any) -> Any:
    """The day asked for, if it is a valid date before today; None means today's live view."""
    from datetime import date as _date

    if not text or today is None:
        return None
    try:
        d = _date.fromisoformat(text)
    except ValueError:
        return None
    return d if d < today else None


def _weekday(d: Any, step: int) -> Any:
    from datetime import timedelta

    x = d + timedelta(days=step)
    while x.weekday() >= 5:
        x += timedelta(days=step)
    return x


def _day_summary(trades: list[Trade]) -> dict[str, Any]:
    closed = [t for t in trades if t.status == "CLOSED"]
    return {"trades": len(closed), "failed": sum(1 for t in trades if t.status == "FAILED"),
            "open": sum(1 for t in trades if t.status in ("OPEN", "EXITING", "PENDING_ENTRY")),
            "wins": sum(1 for t in closed if (t.net_pnl or 0) > 0),
            "gross": round(sum(t.gross_pnl or 0 for t in closed), 2), "net": round(sum(t.net_pnl or 0 for t in closed), 2)}


@router.get("/strategies/{iid}/slots", response_class=HTMLResponse)
async def instance_slots(iid: int, request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    """HTMX partial: today's slots, refreshed every few seconds."""
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        require_visible(user, row.owner_user_id if row else None)
    p = request.app.state.platform
    rt = p.strategies.runtimes.get(iid) if p else None
    snap = rt.snapshot() if rt else None
    views: dict[str, dict[str, Any]] = {}
    totals = {"points": 0.0, "pnl": 0.0, "trades": 0}
    st = (snap or {}).get("strategy") or {}
    refs = [s["trade_id"] for s in (st.get("slots") or []) + (st.get("signals") or []) + (st.get("setups") or []) + (st.get("ce_signals") or [])
            if s.get("trade_id")]
    if refs:
        from app.web.slot_view import slot_trade_view

        with request.app.state.db.session() as s:
            rows = {t.trade_ref: t for t in s.scalars(select(Trade).where(Trade.trade_ref.in_(refs)))}
            data = {ref: {"direction": t.direction, "signal_spot": t.signal_spot, "quantity": t.quantity,
                          "lot_size": t.lot_size, "entry_price": t.entry_price, "exit_price": t.exit_price,
                          "exit_trigger_spot": t.exit_trigger_spot, "gross_pnl": t.gross_pnl, "net_pnl": t.net_pnl,
                          "status": t.status, "exit_time": t.exit_time, "partials": (t.meta or {}).get("partials"),
                          "spot_sl": t.spot_sl, "spot_sl_side": t.spot_sl_side, "exit_reason": t.exit_reason,
                          "tp_premium_level": t.tp_premium_level}
                    for ref, t in rows.items()}
        live_by_ref = {lt.ref: lt for lt in list(p.trades.live.values())} if p else {}
        for ref, tr in data.items():
            lt = live_by_ref.get(ref)
            live = None
            if lt is not None:
                live = {"spot": p.market.ltp(lt.spot_key) if lt.spot_key else None,
                        "premium": p.market.ltp(lt.option_series), "open_qty": lt.open_qty,
                        "spot_sl": lt.spot_sl, "spot_sl_side": lt.spot_sl_side}
                tr = {**tr, "partials": lt.partials, "status": lt.status}
            v = slot_trade_view(tr, live)
            views[ref] = v
            if v.get("points") is not None:
                totals["points"] += v["points"]
            if v.get("pnl") is not None:
                totals["pnl"] += v["pnl"]
            totals["trades"] += 1
    totals = {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}
    closed = None  # weekend / holiday: the table shows the last trading day's slots
    if p is not None:
        today = p.clock.now().date()
        if not p.calendar.is_trading_day(today):
            shown = st.get("day")
            closed = {"reason": p.calendar.closed_reason(today),
                      "day": date_cls.fromisoformat(shown) if shown and shown != today.isoformat() else None}
    return render(request, "partials/slots.html", {"rt": snap, "views": views, "totals": totals, "closed": closed})

