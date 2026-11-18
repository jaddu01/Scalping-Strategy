"""FastAPI application: startup/shutdown of the database and trading engine, security headers,
auth redirects, and router wiring. Frontend: server-rendered Jinja2 templates + HTMX, with a
small vanilla-JS WebSocket client for live updates."""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.auth.deps import LoginRequired, PasswordChangeRequired, TwoFactorRequired, is_https
from app.auth.security import SessionStore, user_count
from app.web.render import render
from core.config import AppConfig
from storage.crypto import Crypto, set_crypto
from storage.db import Database

log = logging.getLogger("algo.app")
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


def create_app(config: AppConfig | None = None, *, start_engine: bool | None = None, clock=None) -> FastAPI:
    config = config or AppConfig.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        set_crypto(Crypto(config.master_key or None))  # raises a clear error if ALGO_MASTER_KEY is missing
        db = Database(config.database_url)
        db.migrate()
        app.state.db = db
        app.state.sessions = SessionStore(db, config.session_idle_minutes, config.session_max_hours)
        app.state.sessions.purge_expired()
        app.state.platform = None
        run_engine = (not config.disable_engine) if start_engine is None else start_engine
        if run_engine:
            from engine.platform import Platform

            platform = Platform(config, db, clock=clock)
            app.state.platform = platform
            await platform.start()
        try:
            yield
        finally:
            if app.state.platform is not None:
                await app.state.platform.stop()
            db.dispose()

    app = FastAPI(title="Algo Trading Platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if (not path.startswith("/static") and path not in ("/setup", "/healthz") and hasattr(request.app.state, "db")
                and not getattr(request.app.state, "has_users", False)):
            if user_count(request.app.state.db) == 0:
                return RedirectResponse("/setup", status_code=303)
            request.app.state.has_users = True
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' ws: wss:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if not path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        if is_https(request):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        nxt = request.url.path if request.method == "GET" else "/"
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    @app.exception_handler(TwoFactorRequired)
    async def _2fa_required(request: Request, exc: TwoFactorRequired):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "two-factor verification required"}, status_code=401)
        return RedirectResponse("/login/totp", status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _pw_change(request: Request, exc: PasswordChangeRequired):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "password change required"}, status_code=403)
        return RedirectResponse("/profile?must_change=1", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/api") or request.headers.get("hx-request"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", {"code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    from app.api import admin_data
    from app.api import routes as api_routes
    from app.api import ws as ws_routes
    from app.web import (routes_accounts, routes_admin, routes_auth, routes_backtest, routes_compare,
                         routes_dashboard, routes_logs, routes_settings, routes_shared, routes_strategies,
                         routes_trades)

    for mod in (routes_auth, routes_dashboard, routes_shared, routes_strategies, routes_compare, routes_accounts, routes_settings, routes_trades,
                routes_logs, routes_backtest, routes_admin, api_routes, admin_data, ws_routes):
        app.include_router(mod.router)
    return app
