"""Global settings, Telegram test, NSE holiday list, YAML export/import (secrets excluded)."""
from __future__ import annotations

from datetime import date
from typing import Any

import yaml
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.deps import SessionInfo, audit, current_user, require_admin
from app.web.forms import build_fields, errors_by_field, parse_form
from app.web.render import redirect, render
from brokers.registry import BROKERS, credentials_model
from core.settings_schema import GlobalSettings
from engine.calendar import parse_holiday_file
from notify.telegram import send_telegram
from storage.models import BrokerAccount, StrategyInstance
from storage.repo import (account_credentials, get_secret, has_secret, holidays, load_global_settings,
                          replace_holidays, save_global_settings, set_secret)
from strategies.registry import get_template

router = APIRouter()


def _choices(request: Request) -> dict[str, list[tuple[str, str]]]:
    with request.app.state.db.session() as s:
        accs = [(str(a.id), f"{a.label} ({a.broker_type})") for a in s.scalars(select(BrokerAccount).order_by(BrokerAccount.label))]
    return {"general.data_feed_account_id": accs}


def _ctx(request: Request, values: Any, errors: dict[str, str] | None = None, general: list[str] | None = None) -> dict[str, Any]:
    db = request.app.state.db
    hol = holidays(db)
    return {"fields": build_fields(GlobalSettings, values, errors, "", _choices(request)),
            "general_errors": general or [], "has_bot_token": has_secret(db, "telegram_bot_token"),
            "holidays_text": "\n".join(f"{d.isoformat()}, {n}" for d, n in sorted(hol.items())),
            "holiday_count": len(hol), "upcoming": [(d, n) for d, n in sorted(hol.items()) if d >= date.today()][:6]}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    return render(request, "settings.html", _ctx(request, load_global_settings(request.app.state.db)))


@router.post("/settings")
async def save_settings(request: Request, user: SessionInfo = Depends(require_admin)) -> Response:
    form = await request.form()
    raw = parse_form(GlobalSettings, form)
    try:
        new = GlobalSettings.model_validate(raw)
    except ValidationError as exc:
        errors, general = errors_by_field(exc)
        return render(request, "settings.html", _ctx(request, raw, errors, general), 400)
    db = request.app.state.db
    diff = save_global_settings(db, new)
    token = (form.get("telegram_bot_token") or "").strip()
    if token:
        set_secret(db, "telegram_bot_token", token)
        diff["notifications.telegram_bot_token"] = ["•••", "••• (changed)"]
    if form.get("clear_bot_token") == "on":
        set_secret(db, "telegram_bot_token", "")
        diff["notifications.telegram_bot_token"] = ["•••", "(cleared)"]
    p = request.app.state.platform
    if p is not None:
        p.apply_settings(new)
    audit(request, user, "settings_changed", "global", diff)
    return redirect("/settings", ok=f"Settings saved ({len(diff)} change{'s' if len(diff) != 1 else ''})")


@router.post("/settings/telegram/test", response_class=HTMLResponse)
async def telegram_test(request: Request, user: SessionInfo = Depends(require_admin)) -> HTMLResponse:
    db = request.app.state.db
    gs = load_global_settings(db)
    ok, msg = await send_telegram(get_secret(db, "telegram_bot_token"), gs.notifications.telegram_chat_id,
                                  f"✅ Test message from the algo platform (sent by {user.username})")
    audit(request, user, "telegram_test", "", {"ok": ok})
    return render(request, "partials/test_result.html", {"res": {"ok": ok, "message": msg}})


@router.post("/settings/holidays")
async def save_holidays(request: Request, text: str = Form(""), user: SessionInfo = Depends(require_admin)) -> Response:
    try:
        items = parse_holiday_file(text)
    except ValueError as exc:
        return redirect("/settings#holidays", err=f"Holiday list not saved: {exc}")
    db = request.app.state.db
    old = holidays(db)
    replace_holidays(db, items)
    p = request.app.state.platform
    if p is not None:
        p.reload_holidays()
    audit(request, user, "holidays_changed", "", {"before": len(old), "after": len(items)})
    return redirect("/settings#holidays", ok=f"{len(items)} holidays saved")


@router.post("/settings/holidays/import")
async def import_holidays(request: Request, file: UploadFile = File(...), mode: str = Form("merge"),
                          user: SessionInfo = Depends(require_admin)) -> Response:
    raw = (await file.read(2_000_000)).decode("utf-8", "ignore")
    try:
        items = parse_holiday_file(raw)
    except ValueError as exc:
        return redirect("/settings#holidays", err=f"Import failed: {exc}")
    db = request.app.state.db
    current = holidays(db) if mode == "merge" else {}
    current.update(items)
    replace_holidays(db, current)
    p = request.app.state.platform
    if p is not None:
        p.reload_holidays()
    audit(request, user, "holidays_imported", file.filename or "", {"imported": len(items), "mode": mode})
    return redirect("/settings#holidays", ok=f"Imported {len(items)} holidays ({mode})")


# ------------------------------------------------------------------ YAML export / import
def export_config(db: Any) -> dict[str, Any]:
    gs = load_global_settings(db)
    with db.session() as s:
        accounts = []
        for a in s.scalars(select(BrokerAccount).order_by(BrokerAccount.id)):
            creds = account_credentials(a)
            secret = credentials_model(a.broker_type).secret_fields() if a.broker_type in BROKERS else set()
            accounts.append({"label": a.label, "broker_type": a.broker_type, "enabled": a.enabled,
                             "auto_relogin": a.auto_relogin, "risk_limits": a.risk_limits,
                             "settings": {k: v for k, v in creds.items() if k not in secret}})
        labels = {a.id: a.label for a in s.scalars(select(BrokerAccount))}
        instances = [{"name": i.name, "template": i.template_id, "mode": i.mode, "enabled": False,
                      "broker_account": labels.get(i.broker_account_id or 0), "params": i.params,
                      "risk_limits": i.risk_limits, "notes": i.notes}
                     for i in s.scalars(select(StrategyInstance).order_by(StrategyInstance.id))]
    return {"version": 1, "global_settings": gs.model_dump(mode="json"),
            "holidays": [{"date": d.isoformat(), "description": n} for d, n in sorted(holidays(db).items())],
            "broker_accounts": accounts, "strategy_instances": instances,
            "_note": "Secrets (API keys, PINs, TOTP secrets, bot tokens) are never exported. Re-enter them after import."}


@router.get("/settings/export.yaml")
async def export_yaml(request: Request, user: SessionInfo = Depends(require_admin)) -> Response:
    data = export_config(request.app.state.db)
    audit(request, user, "settings_exported", "yaml")
    return Response(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), media_type="application/x-yaml",
                    headers={"Content-Disposition": "attachment; filename=algo_settings.yaml"})


@router.post("/settings/import")
async def import_yaml(request: Request, file: UploadFile = File(...), user: SessionInfo = Depends(require_admin)) -> Response:
    try:
        data = yaml.safe_load((await file.read(5_000_000)).decode("utf-8"))
        assert isinstance(data, dict)
    except Exception:
        return redirect("/settings", err="Not a valid settings YAML file")
    db = request.app.state.db
    summary: dict[str, Any] = {}
    try:
        if "global_settings" in data:
            gs = GlobalSettings.model_validate(data["global_settings"])
            summary["global_changes"] = len(save_global_settings(db, gs))
            p = request.app.state.platform
            if p is not None:
                p.apply_settings(gs)
        if "holidays" in data:
            replace_holidays(db, {date.fromisoformat(h["date"]): h.get("description", "") for h in data["holidays"]})
            summary["holidays"] = len(data["holidays"])
        created_acc = created_inst = updated_inst = 0
        with db.session() as s:
            labels = {a.label: a.id for a in s.scalars(select(BrokerAccount))}
            for a in data.get("broker_accounts", []) or []:
                if a.get("label") in labels or a.get("broker_type") not in BROKERS:
                    continue
                from storage.crypto import get_crypto

                row = BrokerAccount(broker_type=a["broker_type"], label=a["label"], enabled=False,
                                    auto_relogin=bool(a.get("auto_relogin", True)), risk_limits=a.get("risk_limits") or {},
                                    credentials_enc=get_crypto().encrypt_json(a.get("settings") or {}),
                                    client_id=str((a.get("settings") or {}).get("client_id", "")))
                s.add(row)
                s.flush()
                labels[row.label] = row.id
                created_acc += 1
            for i in data.get("strategy_instances", []) or []:
                params = get_template(i["template"]).settings_model.model_validate(i.get("params") or {})
                existing = s.scalar(select(StrategyInstance).where(StrategyInstance.name == i["name"]))
                if existing is not None:
                    if existing.enabled:
                        continue  # never change a running instance from an import
                    existing.params = params.model_dump(mode="json")
                    existing.risk_limits = i.get("risk_limits") or {}
                    existing.notes = i.get("notes", "")
                    updated_inst += 1
                else:
                    s.add(StrategyInstance(name=i["name"], template_id=i["template"], mode="paper", enabled=False,
                                           broker_account_id=labels.get(i.get("broker_account")),
                                           params=params.model_dump(mode="json"), risk_limits=i.get("risk_limits") or {},
                                           notes=i.get("notes", "")))
                    created_inst += 1
        summary.update(accounts_created=created_acc, instances_created=created_inst, instances_updated=updated_inst)
    except (ValidationError, KeyError, ValueError) as exc:
        return redirect("/settings", err=f"Import failed: {exc}")
    p = request.app.state.platform
    if p is not None:
        p.reload_holidays()
    audit(request, user, "settings_imported", file.filename or "", summary)
    return redirect("/settings", ok="Imported: " + ", ".join(f"{k}={v}" for k, v in summary.items()) +
                    ". Imported instances are in paper mode and disabled; re-enter account secrets.")
