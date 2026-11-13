"""Template rendering with common context (user, CSRF token, navigation, flash messages)."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.deps import get_session
from core.clock import IST

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

NAV = [
    ("/", "Dashboard", False),
    ("/strategies", "Strategies", False),
    ("/compare", "Compare", False),
    ("/accounts", "Accounts", False),
    ("/trades", "Trades", False),
    ("/logs", "Logs", False),
    ("/backtest", "Backtest", False),
    ("/settings", "Settings", False),
    ("/users", "Users", True),
    ("/audit", "Audit", False),
]


def _fmt_dt(v: Any, fmt: str = "%d-%b %H:%M:%S") -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(IST)
        return v.strftime(fmt)
    if isinstance(v, date):
        return v.strftime("%d-%b-%Y")
    return str(v)


def _money(v: Any, digits: int = 2) -> str:
    if v is None or v == "":
        return "–"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _pnl_class(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return "pos" if f > 0 else "neg" if f < 0 else ""


def _asset_version() -> str:
    """Changes whenever app.css / app.js change, so browsers fetch the new files after a deploy."""
    import hashlib

    static = Path(__file__).resolve().parent / "static"
    h = hashlib.sha1()
    for name in ("app.css", "app.js"):
        try:
            h.update((static / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()
def _regime_short(note: Any) -> str:
    """Table label for a sideways-filter note: "ok", "would block (2)" or "BLOCK (3)"; the full note goes in a tooltip."""
    note = str(note or "")
    if not note:
        return ""
    if note.startswith("ok"):
        return "ok"
    head, _, reasons = note.partition(":")
    n = len([r for r in reasons.split(";") if r.strip()])
    return f"{head.strip()} ({n})" if n else head.strip()


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["regime_short"] = _regime_short
templates.env.filters["money"] = _money
templates.env.filters["pnlc"] = _pnl_class
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, default=str, ensure_ascii=False)


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    info = get_session(request) if hasattr(request.app.state, "sessions") else None
    user = info if info and not info.pending_2fa else None
    base = {
        "request": request,
        "user": user,
        "csrf_token": info.csrf_token if info else request.cookies.get("algo_csrf", ""),
        "nav": [(href, label) for href, label, admin_only in NAV if not admin_only or (user and user.role == "admin")],
        "path": request.url.path,
        "ok": request.query_params.get("ok"),
        "err": request.query_params.get("err"),
        "is_admin": bool(user and user.role == "admin"),
        "can_trade": bool(user and user.role in ("admin", "trader")),
        "me_id": user.user_id if user else None,
    }
    base.update(ctx or {})
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def redirect(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode

    params = {k: v for k, v in (("ok", ok), ("err", err)) if v}
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (sep + urlencode(params) if params else ""), status_code=303)
