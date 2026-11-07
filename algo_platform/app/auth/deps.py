"""FastAPI dependencies: current user, role checks, CSRF enforcement, client IP, audit helper."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from app.auth.security import SessionInfo, csrf_equal
from storage.repo import audit as _audit

SESSION_COOKIE = "algo_session"
PREAUTH_CSRF_COOKIE = "algo_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginRequired(Exception):
    """Raised for unauthenticated browser requests; turned into a redirect to /login."""


class TwoFactorRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


def client_ip(request: Request) -> str:
    cfg = request.app.state.config
    if cfg.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    cfg = request.app.state.config
    return cfg.trust_proxy_headers and request.headers.get("x-forwarded-proto", "").lower() == "https"


def cookie_secure(request: Request) -> bool:
    mode = request.app.state.config.cookie_secure
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("false", "0", "no"):
        return False
    return is_https(request)


def get_session(request: Request) -> SessionInfo | None:
    cached = getattr(request.state, "session_info", "unset")
    if cached != "unset":
        return cached
    info = request.app.state.sessions.get(request.cookies.get(SESSION_COOKIE))
    request.state.session_info = info
    return info


async def check_csrf(request: Request, info: SessionInfo) -> None:
    if request.method in SAFE_METHODS:
        return
    token = request.headers.get("x-csrf-token")
    if not token:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            form = await request.form()
            token = form.get("csrf_token")  # type: ignore[assignment]
    if not csrf_equal(token, info.csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")


async def current_user(request: Request) -> SessionInfo:
    info = get_session(request)
    if info is None:
        raise LoginRequired()
    if info.pending_2fa:
        raise TwoFactorRequired()
    if info.must_change_password and request.url.path not in ("/profile", "/profile/password", "/logout"):
        raise PasswordChangeRequired()
    await check_csrf(request, info)
    return info


async def require_admin(user: SessionInfo = Depends(current_user)) -> SessionInfo:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


def audit(request: Request, user: SessionInfo | None, action: str, target: str = "",
          details: dict[str, Any] | None = None) -> None:
    db = request.app.state.db
    _audit(db, user.username if user else "anonymous", action, target, details, client_ip(request))
    platform = getattr(request.app.state, "platform", None)
    if platform is not None:
        from core.events import Category

        platform.bus.emit(Category.AUDIT, action.upper(), f"{user.username if user else 'anonymous'}: {action} {target}".strip(),
                          by=user.username if user else None, target=target, details=details or {})
