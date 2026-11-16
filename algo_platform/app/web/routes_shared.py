"""Shared strategies: the rules page, editing the rules (applied to every partner's run), starting your
own run of a strategy, making your own version of it, and deleting it."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.access import can_control, can_trade, request_scope, require_trader, usernames
from app.auth.deps import SessionInfo, audit, current_user
from app.web.forms import errors_by_field, parse_form
from app.web.render import redirect, render
from app.web.routes_strategies import _form_ctx, _parse_instance_form
from app.web.shared import PERSONAL_PARAMS, check_runs, lots_of, merged
from core.settings_schema import InstanceRiskLimits
from storage.models import BrokerAccount, SharedStrategy, StrategyInstance
from storage.repo import diff_dicts
from strategies.registry import get_template

router = APIRouter()


def _get(request: Request, sid: int) -> SharedStrategy:
    with request.app.state.db.session() as s:
        sh = s.get(SharedStrategy, sid)
        if sh is None:
            raise HTTPException(404, "strategy not found")
        s.expunge(sh)
    return sh


def _for_rules(request: Request, user: SessionInfo, sid: int) -> SharedStrategy:
    sh = _get(request, sid)
    if not can_control(user, sh.created_by):
        raise HTTPException(403, "only the admin (or whoever made this version) can change its rules — "
                                 "use “Make my own version” to change them for yourself")
    return sh


def _runs(request: Request, sid: int) -> list[StrategyInstance]:
    with request.app.state.db.session() as s:
        rows = list(s.scalars(select(StrategyInstance).where(StrategyInstance.strategy_id == sid)
                              .order_by(StrategyInstance.name)))
        for r in rows:
            s.expunge(r)
    return rows


def _unique(request: Request, model: Any, base: str) -> str:
    name, n = base, 2
    with request.app.state.db.session() as s:
        while s.scalar(select(model).where(model.name == name)):
            name, n = f"{base} {n}", n + 1
    return name


def _rules_values(sh: SharedStrategy) -> dict[str, Any]:
    model = get_template(sh.template_id).settings_model
    return model.model_validate(merged(model, sh.params, {})).model_dump()


# ------------------------------------------------------------------ the rules page
@router.get("/strategies/shared/{sid}", response_class=HTMLResponse)
async def shared_detail(sid: int, request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    sh = _get(request, sid)
    p = request.app.state.platform
    names = usernames(request.app.state.db)
    with request.app.state.db.session() as s:
        accounts = {a.id: a.label for a in s.scalars(select(BrokerAccount))}
    today = p.day_summary()["instances"] if p is not None else {}
    runs = []
    sc = request_scope(request, user)
    for r in _runs(request, sid):
        if not sc.instance(r.id):
            continue  # other partners' runs of these rules are private
        rt = p.strategies.runtimes.get(r.id) if p else None
        b = today.get(r.id, {})
        runs.append({"row": r, "owner": names.get(r.owner_user_id or 0, "–"),
                     "account": accounts.get(r.broker_account_id or 0, "–"), "lots": lots_of(r.params),
                     "status": rt.status if rt else r.status, "can_edit": can_control(user, r.owner_user_id),
                     "open": bool(p and p.trades.open_trade_count(instance_id=r.id)),
                     "trades": b.get("trades", 0), "net": b.get("net", 0.0)})
    rules = {k: v for k, v in get_template(sh.template_id).settings_model.model_validate(
        merged(get_template(sh.template_id).settings_model, sh.params, {})).model_dump(mode="json").items()
        if k not in PERSONAL_PARAMS}
    return render(request, "shared_detail.html", {
        "sh": sh, "template": get_template(sh.template_id), "rules": rules, "runs": runs,
        "rules_by": names.get(sh.created_by or 0, "admin"), "can_edit_rules": can_control(user, sh.created_by),
        "mine": any(r["row"].owner_user_id == user.user_id for r in runs), "can_run": can_trade(user)})


# ------------------------------------------------------------------ edit the rules (everyone's runs follow)
@router.get("/strategies/shared/{sid}/edit", response_class=HTMLResponse)
async def edit_rules(sid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    sh = _for_rules(request, user, sid)
    values = {"name": sh.name, "notes": sh.notes, "params": _rules_values(sh)}
    return render(request, "strategy_form.html", _form_ctx(request, user, sh.template_id, values, kind="rules",
                                                           shared=sh))


@router.post("/strategies/shared/{sid}/edit")
async def save_rules(sid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    sh = _for_rules(request, user, sid)
    model = get_template(sh.template_id).settings_model
    form = await request.form()
    raw = parse_form(model, form, "params.")
    values = {"name": (form.get("name") or "").strip(), "notes": form.get("notes") or "", "params": raw}
    errors: dict[str, str] = {}
    general: list[str] = []
    rules: dict[str, Any] | None = None
    try:
        rules = model.model_validate(merged(model, raw, {})).model_dump(mode="json")
    except ValidationError as exc:
        e, g = errors_by_field(exc, "params.")
        errors.update(e)
        general += g
    if not values["name"]:
        errors["name"] = "required"
    with request.app.state.db.session() as s:
        if values["name"] and s.scalar(select(SharedStrategy).where(SharedStrategy.name == values["name"],
                                                                    SharedStrategy.id != sid)):
            errors["name"] = "a strategy with this name already exists"
    runs = _runs(request, sid)
    new_params: dict[str, dict[str, Any]] = {}
    if rules is not None:
        new_params, problems = check_runs(model, rules, [(r.name, r.params or {}) for r in runs])
        general += [f"does not fit the run {pr} — change that run's lots first" for pr in problems]
    if errors or general:
        return render(request, "strategy_form.html", _form_ctx(request, user, sh.template_id, values, errors, general,
                                                               kind="rules", shared=sh), 400)
    changes = diff_dicts({"name": sh.name, "notes": sh.notes, "params": sh.params},
                         {"name": values["name"], "notes": values["notes"], "params": rules})
    with request.app.state.db.session() as s:
        row = s.get(SharedStrategy, sid)
        row.name, row.notes, row.params = values["name"], values["notes"], rules
        for r in runs:
            s.get(StrategyInstance, r.id).params = new_params[r.name]
    audit(request, user, "strategy_rules_updated", values["name"], {**changes, "runs": [r.name for r in runs]})
    p = request.app.state.platform
    live = 0
    if p is not None:
        for r in runs:
            if r.enabled and r.id in p.strategies.runtimes:
                await p.strategies.update_params(r.id, new_params[r.name])
                live += 1
    msg = f"Rules saved for {len(runs)} run(s)."
    if live:
        msg += f" {live} running: the change applies from each one's next trade slot."
    return redirect(f"/strategies/shared/{sid}", ok=msg)


# ------------------------------------------------------------------ run it on my own account
@router.get("/strategies/shared/{sid}/run", response_class=HTMLResponse)
async def new_run(sid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    sh = _get(request, sid)
    values = {"name": _unique(request, StrategyInstance, f"{sh.name} – {user.username}"), "mode": "paper",
              "notes": "", "broker_account_id": None, "owner_user_id": user.user_id, "params": _rules_values(sh),
              "risk_limits": InstanceRiskLimits().model_dump()}
    return render(request, "strategy_form.html", _form_ctx(request, user, sh.template_id, values, kind="run",
                                                           shared=sh))


@router.post("/strategies/shared/{sid}/run")
async def create_run(sid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    sh = _get(request, sid)
    values, errors, general, params, risk = await _parse_instance_form(request, sh.template_id, user, rules=sh.params)
    with request.app.state.db.session() as s:
        if values["name"] and s.scalar(select(StrategyInstance).where(StrategyInstance.name == values["name"])):
            errors["name"] = "a run with this name already exists"
    if errors or general:
        return render(request, "strategy_form.html", _form_ctx(request, user, sh.template_id, values, errors, general,
                                                               kind="run", shared=sh), 400)
    with request.app.state.db.session() as s:
        row = StrategyInstance(name=values["name"], template_id=sh.template_id, params=params.model_dump(mode="json"),
                               broker_account_id=values["broker_account_id"], mode=values["mode"], enabled=False,
                               risk_limits=risk.model_dump(mode="json"), notes=values["notes"],
                               owner_user_id=values["owner_user_id"], strategy_id=sid)
        s.add(row)
        s.flush()
        iid = row.id
    audit(request, user, "run_created", values["name"], {"strategy": sh.name, "mode": values["mode"],
                                                         "owner_user_id": values["owner_user_id"],
                                                         "lots": lots_of(params.model_dump(mode="json"))})
    return redirect(f"/strategies/{iid}", ok="Your run is created (disabled). Switch it on when you are ready.")


# ------------------------------------------------------------------ my own version / delete
@router.post("/strategies/shared/{sid}/fork")
async def fork(sid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    """A copy of the rules that the user can change; the original stays as it is for everyone else."""
    sh = _get(request, sid)
    name = _unique(request, SharedStrategy, f"{sh.name} ({user.username})")
    with request.app.state.db.session() as s:
        new = SharedStrategy(name=name, template_id=sh.template_id, params=dict(sh.params), notes=sh.notes,
                             created_by=user.user_id)
        s.add(new)
        s.flush()
        nid = new.id
    audit(request, user, "strategy_forked", name, {"from": sh.name})
    return redirect(f"/strategies/shared/{nid}/edit", ok="Your own version — change the rules, save, then run it")


@router.post("/strategies/shared/{sid}/delete")
async def delete_shared(sid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    sh = _for_rules(request, user, sid)
    runs = _runs(request, sid)
    if runs:
        return redirect(f"/strategies/shared/{sid}", err="Delete the runs first: " + ", ".join(r.name for r in runs))
    with request.app.state.db.session() as s:
        s.delete(s.get(SharedStrategy, sid))
    audit(request, user, "strategy_deleted", sh.name)
    return redirect("/strategies", ok=f"Deleted {sh.name}")
