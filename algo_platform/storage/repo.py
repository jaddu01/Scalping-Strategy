"""Small data-access helpers shared by the engine and the web app."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.settings_schema import GlobalSettings
from storage.crypto import get_crypto
from storage.db import Database
from storage.models import AuditLog, BrokerAccount, Holiday, PaperState, Setting, Trade

GLOBAL_KEY = "global"
SECRET_PREFIX = "secret:"


# ------------------------------------------------------------------ settings
def load_global_settings(db: Database) -> GlobalSettings:
    with db.session() as s:
        row = s.get(Setting, GLOBAL_KEY)
        if row is None or not row.value:
            return GlobalSettings()
        try:
            return GlobalSettings.model_validate(row.value)
        except Exception:
            # unknown/old keys: keep what still validates, fall back to defaults otherwise
            base = GlobalSettings().model_dump(mode="json")
            merged = _deep_merge(base, row.value)
            try:
                return GlobalSettings.model_validate(merged)
            except Exception:
                return GlobalSettings()


def save_global_settings(db: Database, new: GlobalSettings) -> dict[str, Any]:
    """Persist and return a {path: [old, new]} diff for the audit log."""
    with db.session() as s:
        row = s.get(Setting, GLOBAL_KEY)
        old = row.value if row else GlobalSettings().model_dump(mode="json")
        new_json = new.model_dump(mode="json")
        if row is None:
            s.add(Setting(key=GLOBAL_KEY, value=new_json))
        else:
            row.value = new_json
    return diff_dicts(old, new_json)


def get_secret(db: Database, name: str) -> str:
    with db.session() as s:
        row = s.get(Setting, SECRET_PREFIX + name)
        if row is None or not row.value.get("enc"):
            return ""
        return get_crypto().decrypt(row.value["enc"])


def set_secret(db: Database, name: str, value: str) -> None:
    with db.session() as s:
        row = s.get(Setting, SECRET_PREFIX + name)
        payload = {"enc": get_crypto().encrypt(value)} if value else {}
        if row is None:
            s.add(Setting(key=SECRET_PREFIX + name, value=payload))
        else:
            row.value = payload


def has_secret(db: Database, name: str) -> bool:
    with db.session() as s:
        row = s.get(Setting, SECRET_PREFIX + name)
        return bool(row and row.value.get("enc"))


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def diff_dicts(old: Any, new: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            out.update(diff_dicts(old.get(k), new.get(k), f"{prefix}{k}."))
        return out
    if old != new:
        out[prefix.rstrip(".")] = [old, new]
    return out


# ------------------------------------------------------------------ audit
def audit(db: Database, username: str, action: str, target: str = "", details: dict[str, Any] | None = None,
          ip: str | None = None) -> None:
    from core.masking import mask_secrets

    with db.session() as s:
        s.add(AuditLog(username=username or "system", action=action, target=target,
                       details=mask_secrets(details or {}), ip=ip))


# ------------------------------------------------------------------ holidays
def holidays(db: Database) -> dict[date, str]:
    with db.session() as s:
        return {h.day: h.description for h in s.scalars(select(Holiday).order_by(Holiday.day))}


def replace_holidays(db: Database, items: dict[date, str]) -> None:
    with db.session() as s:
        for h in s.scalars(select(Holiday)):
            s.delete(h)
        s.flush()
        for d, desc in sorted(items.items()):
            s.add(Holiday(day=d, description=desc or ""))


# ------------------------------------------------------------------ trades
def next_trade_ref(s: Session) -> str:
    n = s.scalar(select(func.max(Trade.id))) or 0
    return f"T{n + 1:06d}"


# ------------------------------------------------------------------ accounts
def account_credentials(acc: BrokerAccount) -> dict[str, Any]:
    return get_crypto().decrypt_json(acc.credentials_enc) if acc.credentials_enc else {}


# ------------------------------------------------------------------ paper state
def load_paper_state(db: Database, key: str) -> dict[str, Any]:
    with db.session() as s:
        row = s.get(PaperState, key)
        return dict(row.state) if row else {}


def save_paper_state(db: Database, key: str, state: dict[str, Any]) -> None:
    with db.session() as s:
        row = s.get(PaperState, key)
        if row is None:
            s.add(PaperState(key=key, state=state))
        else:
            row.state = state
