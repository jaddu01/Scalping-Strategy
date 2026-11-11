"""Broker accounts: credentials (encrypted at rest, only ever shown masked), connection test,
status, data-feed selection, reconnect, delete."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.access import (account_for_control, can_control, is_admin, owner_choices, owner_filter,
                             require_trader, resolve_owner, usernames)
from app.auth.deps import SessionInfo, audit, current_user, require_admin
from app.web.forms import build_fields, errors_by_field, parse_form
from app.web.render import redirect, render
from brokers.registry import BROKERS, credentials_model, is_stub
from core.masking import mask_value
from core.settings_schema import AccountRiskLimits
from storage.crypto import get_crypto
from storage.models import BrokerAccount, StrategyInstance
from storage.repo import account_credentials, load_global_settings, save_global_settings

router = APIRouter()


def _broker_choices() -> list[tuple[str, str, bool]]:
    return [(k, cls.display_name, is_stub(k)) for k, cls in BROKERS.items() if k != "mock"]


def _ctx(broker_type: str, values: dict[str, Any], errors: dict[str, str] | None = None,
         general: list[str] | None = None, account: BrokerAccount | None = None,
         saved: set[str] | None = None, owners: list[tuple[int, str]] | None = None) -> dict[str, Any]:
    model = credentials_model(broker_type)
    return {"owners": owners or [],"broker_type": broker_type, "broker_name": BROKERS[broker_type].display_name,
            "stub": is_stub(broker_type), "brokers": _broker_choices(),
            "cred_fields": build_fields(model, values.get("credentials"), errors, "cred.", saved_secrets=saved),
            "risk_fields": build_fields(AccountRiskLimits, values.get("risk"), errors, "risk."),
            "values": values, "errors": errors or {}, "general_errors": general or [], "account": account}


@router.get("/accounts", response_class=HTMLResponse)
async def list_accounts(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    gs = load_global_settings(request.app.state.db)
    owner_val, owner_id = owner_filter(request, user)
    names = usernames(request.app.state.db)
    with request.app.state.db.session() as s:
        stmt = select(BrokerAccount).order_by(BrokerAccount.label)
        if owner_id is not None:
            stmt = stmt.where(BrokerAccount.owner_user_id == owner_id)
        rows = list(s.scalars(stmt))
        usage = {}
        for inst in s.scalars(select(StrategyInstance)):
            usage.setdefault(inst.broker_account_id, []).append(inst.name)
    items = []
    for r in rows:
        rt = p.accounts.get(r.id) if p else None
        snap = rt.snapshot() if rt else None
        items.append({"row": r, "rt": snap, "instances": usage.get(r.id, []),
                      "indicator": rt.indicator(False) if rt else ("red" if r.enabled else "grey"),
                      "is_feed": bool(p and p.market.feed_account_id == r.id),
                      "designated_feed": gs.general.data_feed_account_id == r.id,
                      "stub": is_stub(r.broker_type),
                      "broker_name": BROKERS[r.broker_type].display_name if r.broker_type in BROKERS else r.broker_type,
                      "client_id_masked": r.client_id,
                      "owner": names.get(r.owner_user_id or 0, "–"),
                      "can_edit": can_control(user, r.owner_user_id)})
    return render(request, "accounts.html", {"items": items, "brokers": _broker_choices(), "owner": owner_val,
                                             "owners": owner_choices(request.app.state.db)})


@router.get("/accounts/new", response_class=HTMLResponse)
async def new_account(request: Request, broker: str = "angelone", user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    if broker not in BROKERS:
        raise HTTPException(404, "unknown broker")
    values = {"label": "", "enabled": True, "auto_relogin": True, "credentials": {}, "risk": AccountRiskLimits().model_dump(),
              "owner_user_id": user.user_id}
    return render(request, "account_form.html", _ctx(broker, values, owners=_owners(request, user)))


def _owners(request: Request, user: SessionInfo) -> list[tuple[int, str]]:
    """The admin's "Owner" select; partners always own their own accounts (no select)."""
    return owner_choices(request.app.state.db) if is_admin(user) else []


async def _parse(request: Request, broker_type: str, existing: dict[str, Any] | None, user: SessionInfo,
                 current_owner: int | None = None) -> tuple[dict[str, Any], dict[str, str], list[str], Any, Any]:
    form = await request.form()
    model = credentials_model(broker_type)
    raw_creds = parse_form(model, form, "cred.", keep_secrets=existing or {})
    raw_risk = parse_form(AccountRiskLimits, form, "risk.")
    values = {"label": (form.get("label") or "").strip(), "enabled": form.get("enabled") == "on",
              "auto_relogin": form.get("auto_relogin") == "on", "credentials": raw_creds, "risk": raw_risk,
              "owner_user_id": resolve_owner(form.get("owner_user_id"), user, request.app.state.db, current_owner)}
    errors: dict[str, str] = {}
    general: list[str] = []
    creds = risk = None
    try:
        creds = model.model_validate(raw_creds)
    except ValidationError as exc:
        e, g = errors_by_field(exc, "cred.")
        errors.update(e)
        general += g
    try:
        risk = AccountRiskLimits.model_validate(raw_risk)
    except ValidationError as exc:
        e, g = errors_by_field(exc, "risk.")
        errors.update(e)
        general += g
    if not values["label"]:
        errors["label"] = "required"
    return values, errors, general, creds, risk


def _ip_problem(request: Request, broker_type: str, creds: Any, aid: int | None) -> str | None:
    """Angel One: the static IP must be one of this server's IPs and not used by another account
    (SEBI / Angel One: one static IP per client code)."""
    if broker_type != "angelone" or creds is None:
        return None
    from brokers import egress

    if not egress.enabled():
        return None
    problem = egress.route_problem(creds.client_public_ip)
    if problem:
        return problem
    mine = egress.expected_ip(creds.client_public_ip)
    with request.app.state.db.session() as s:
        others = list(s.scalars(select(BrokerAccount).where(BrokerAccount.broker_type == "angelone")))
    for other in others:
        if other.id == aid:
            continue
        if egress.expected_ip(account_credentials(other).get("client_public_ip")) == mine:
            return f"{mine} is already used by {other.label}; each Angel client code needs its own static IP"
    return None


def _client_id(creds: dict[str, Any]) -> str:
    for k in ("client_id", "user_id", "client_code"):
        if creds.get(k):
            return str(creds[k])
    return ""


def _masked(creds: dict[str, Any], secret: set[str]) -> dict[str, Any]:
    return {k: (mask_value(v) if k in secret else v) for k, v in creds.items()}


@router.post("/accounts/new")
async def create_account(request: Request, broker: str = "angelone", user: SessionInfo = Depends(require_trader)) -> Response:
    if broker not in BROKERS:
        raise HTTPException(404, "unknown broker")
    values, errors, general, creds, risk = await _parse(request, broker, None, user)
    with request.app.state.db.session() as s:
        if values["label"] and s.scalar(select(BrokerAccount).where(BrokerAccount.label == values["label"])):
            errors["label"] = "an account with this label already exists"
    ip_err = _ip_problem(request, broker, creds, None)
    if ip_err:
        errors["cred.client_public_ip"] = ip_err
    if errors or general:
        values["credentials"] = {k: ("" if k in credentials_model(broker).secret_fields() else v)
                                 for k, v in values["credentials"].items()}
        return render(request, "account_form.html", _ctx(broker, values, errors, general, owners=_owners(request, user)), 400)
    data = creds.model_dump(mode="json")
    with request.app.state.db.session() as s:
        row = BrokerAccount(broker_type=broker, label=values["label"], client_id=_client_id(data),
                            credentials_enc=get_crypto().encrypt_json(data), enabled=values["enabled"],
                            auto_relogin=values["auto_relogin"], risk_limits=risk.model_dump(mode="json"),
                            owner_user_id=values["owner_user_id"])
        s.add(row)
        s.flush()
        aid = row.id
    audit(request, user, "account_created", values["label"],
          {"broker": broker, "owner_user_id": values["owner_user_id"],
           "credentials": _masked(data, credentials_model(broker).secret_fields())})
    p = request.app.state.platform
    if p is not None:
        await p.reload_account(aid)
    return redirect("/accounts", ok=f"Saved {values['label']}. Use “Test connection” to verify the login.")


@router.get("/accounts/{aid}/edit", response_class=HTMLResponse)
async def edit_account(aid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    row = account_for_control(request, user, aid)
    creds = account_credentials(row)
    secret = credentials_model(row.broker_type).secret_fields()
    saved = {f"cred.{k}" for k in secret if creds.get(k)}
    shown = {k: v for k, v in creds.items() if k not in secret}
    values = {"label": row.label, "enabled": row.enabled, "auto_relogin": row.auto_relogin, "credentials": shown,
              "risk": AccountRiskLimits.model_validate(row.risk_limits or {}).model_dump(),
              "owner_user_id": row.owner_user_id}
    ctx = _ctx(row.broker_type, values, account=row, saved=saved, owners=_owners(request, user))
    ctx["masked"] = {k: mask_value(creds.get(k)) for k in secret if creds.get(k)}
    return render(request, "account_form.html", ctx)


@router.post("/accounts/{aid}/edit")
async def save_account(aid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    row = account_for_control(request, user, aid)
    existing = account_credentials(row)
    values, errors, general, creds, risk = await _parse(request, row.broker_type, existing, user, row.owner_user_id)
    with request.app.state.db.session() as s:
        dup = s.scalar(select(BrokerAccount).where(BrokerAccount.label == values["label"], BrokerAccount.id != aid))
        if dup:
            errors["label"] = "an account with this label already exists"
        if values["owner_user_id"] != row.owner_user_id:
            linked = list(s.scalars(select(StrategyInstance.name).where(
                StrategyInstance.broker_account_id == aid, StrategyInstance.owner_user_id != values["owner_user_id"])))
            if linked:
                errors["owner_user_id"] = (f"strategies of the current owner use this account ({', '.join(linked)}); "
                                           "re-link them first")
    ip_err = _ip_problem(request, row.broker_type, creds, aid)
    if ip_err:
        errors["cred.client_public_ip"] = ip_err
    secret = credentials_model(row.broker_type).secret_fields()
    if errors or general:
        values["credentials"] = {k: v for k, v in values["credentials"].items() if k not in secret}
        saved = {f"cred.{k}" for k in secret if existing.get(k)}
        return render(request, "account_form.html", _ctx(row.broker_type, values, errors, general, row, saved,
                                                          owners=_owners(request, user)), 400)
    data = creds.model_dump(mode="json")
    changed_secrets = sorted(k for k in secret if data.get(k) != existing.get(k))
    changed_plain = {k: [existing.get(k), data.get(k)] for k in data if k not in secret and data.get(k) != existing.get(k)}
    if values["owner_user_id"] != row.owner_user_id:
        changed_plain["owner_user_id"] = [row.owner_user_id, values["owner_user_id"]]
    with request.app.state.db.session() as s:
        r = s.get(BrokerAccount, aid)
        r.label, r.enabled, r.auto_relogin = values["label"], values["enabled"], values["auto_relogin"]
        r.credentials_enc = get_crypto().encrypt_json(data)
        r.client_id = _client_id(data)
        r.risk_limits = risk.model_dump(mode="json")
        r.owner_user_id = values["owner_user_id"]
    audit(request, user, "account_updated", values["label"],
          {"changed_fields": changed_plain, "secrets_changed": changed_secrets})
    p = request.app.state.platform
    if p is not None:
        try:
            await p.reload_account(aid)
        except RuntimeError as exc:
            return redirect("/accounts", err=f"Saved, but not reloaded: {exc}")
    return redirect("/accounts", ok="Saved")


@router.post("/accounts/{aid}/delete")
async def delete_account(aid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    account_for_control(request, user, aid)
    p = request.app.state.platform
    with request.app.state.db.session() as s:
        row = s.get(BrokerAccount, aid)
        if row is None:
            raise HTTPException(404, "account not found")
        used = list(s.scalars(select(StrategyInstance.name).where(StrategyInstance.broker_account_id == aid)))
        if used:
            return redirect("/accounts", err=f"Account is used by: {', '.join(used)}. Re-link or delete those first.")
        if p is not None and p.trades.open_trade_count(account_id=aid):
            return redirect("/accounts", err="Account has open positions")
        label = row.label
        s.delete(row)
    if p is not None:
        await p.remove_account(aid)
    gs = load_global_settings(request.app.state.db)
    if gs.general.data_feed_account_id == aid:
        gs.general.data_feed_account_id = None
        save_global_settings(request.app.state.db, gs)
        if p is not None:
            p.apply_settings(gs)
    audit(request, user, "account_deleted", label)
    return redirect("/accounts", ok=f"Deleted {label}")


@router.post("/accounts/{aid}/test", response_class=HTMLResponse)
async def test_account(aid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> HTMLResponse:
    account_for_control(request, user, aid)
    p = request.app.state.platform
    if p is None:
        res = {"ok": False, "message": "trading engine is not running"}
    else:
        res = await p.test_connection(aid)
    audit(request, user, "account_tested", str(aid), {"ok": res.get("ok"), "message": res.get("message")})
    return render(request, "partials/test_result.html", {"res": res})


@router.post("/accounts/{aid}/feed")
async def set_feed(aid: int, request: Request, user: SessionInfo = Depends(require_admin)) -> Response:
    db = request.app.state.db
    gs = load_global_settings(db)
    old = gs.general.data_feed_account_id
    gs.general.data_feed_account_id = aid
    save_global_settings(db, gs)
    p = request.app.state.platform
    if p is not None:
        p.apply_settings(gs)
    audit(request, user, "data_feed_changed", str(aid), {"general.data_feed_account_id": [old, aid]})
    return redirect("/accounts", ok="Data feed account updated")


@router.post("/accounts/{aid}/reconnect")
async def reconnect(aid: int, request: Request, user: SessionInfo = Depends(require_trader)) -> Response:
    account_for_control(request, user, aid)
    p = request.app.state.platform
    if p is None:
        return redirect("/accounts", err="engine not running")
    ok = await p.connect_account(aid)
    audit(request, user, "account_reconnect", str(aid), {"ok": ok})
    return redirect("/accounts", ok="Reconnected" if ok else None, err=None if ok else "Login failed — see logs")
