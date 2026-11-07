"""Users (admin only) and the audit log."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from app.auth import security as sec
from app.auth.deps import SessionInfo, audit, current_user, require_admin
from app.web.render import redirect, render
from core.clock import combine
from storage.models import AuditLog, User

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, user: SessionInfo = Depends(require_admin)) -> HTMLResponse:
    with request.app.state.db.session() as s:
        rows = list(s.scalars(select(User).order_by(User.username)))
    return render(request, "users.html", {"rows": rows, "now": sec.utcnow()})


@router.post("/users/new")
async def create_user(request: Request, username: str = Form(...), password: str = Form(...),
                      role: str = Form("viewer"), user: SessionInfo = Depends(require_admin)) -> Response:
    username = username.strip().lower()
    if role not in ("admin", "viewer"):
        return redirect("/users", err="role must be admin or viewer")
    if not username or not username.replace("_", "").replace(".", "").isalnum():
        return redirect("/users", err="username: letters, digits, '.' and '_' only")
    problems = sec.password_problems(password, username)
    if problems:
        return redirect("/users", err="Password " + "; ".join(problems))
    with request.app.state.db.session() as s:
        if s.scalar(select(User).where(User.username == username)):
            return redirect("/users", err="username already exists")
        s.add(User(username=username, password_hash=sec.hash_password(password), role=role, must_change_password=True))
    audit(request, user, "user_created", username, {"role": role})
    return redirect("/users", ok=f"Created {username} ({role}); they must change the password at first login")


def _admins(s) -> int:  # type: ignore[no-untyped-def]
    return s.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0


@router.post("/users/{uid}/role")
async def set_role(uid: int, request: Request, role: str = Form(...), user: SessionInfo = Depends(require_admin)) -> Response:
    if role not in ("admin", "viewer"):
        raise HTTPException(400, "bad role")
    with request.app.state.db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404, "user not found")
        if u.role == "admin" and role != "admin" and _admins(s) <= 1:
            return redirect("/users", err="cannot demote the last admin")
        old, u.role = u.role, role
        name = u.username
    request.app.state.sessions.destroy_user_sessions(uid)
    audit(request, user, "user_role_changed", name, {"role": [old, role]})
    return redirect("/users", ok=f"{name} is now {role}")


@router.post("/users/{uid}/reset")
async def reset_password(uid: int, request: Request, password: str = Form(...),
                         user: SessionInfo = Depends(require_admin)) -> Response:
    with request.app.state.db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404, "user not found")
        problems = sec.password_problems(password, u.username)
        if problems:
            return redirect("/users", err="Password " + "; ".join(problems))
        u.password_hash = sec.hash_password(password)
        u.must_change_password = uid != user.user_id
        u.failed_attempts, u.locked_until = 0, None
        name = u.username
    request.app.state.sessions.destroy_user_sessions(uid, except_sid=user.session_id if uid == user.user_id else None)
    audit(request, user, "user_password_reset", name)
    return redirect("/users", ok=f"Password reset for {name}")


@router.post("/users/{uid}/active")
async def set_active(uid: int, request: Request, active: str = Form(...), user: SessionInfo = Depends(require_admin)) -> Response:
    flag = active == "1"
    with request.app.state.db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404, "user not found")
        if uid == user.user_id and not flag:
            return redirect("/users", err="you cannot deactivate yourself")
        if not flag and u.role == "admin" and _admins(s) <= 1:
            return redirect("/users", err="cannot deactivate the last admin")
        u.is_active = flag
        if flag:
            u.locked_until, u.failed_attempts = None, 0
        name = u.username
    if not flag:
        request.app.state.sessions.destroy_user_sessions(uid)
    audit(request, user, "user_activated" if flag else "user_deactivated", name)
    return redirect("/users", ok=f"{name} {'activated' if flag else 'deactivated'}")


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    q = request.query_params
    try:
        d_from = date.fromisoformat(q.get("day_from", ""))
    except ValueError:
        d_from = date.today() - timedelta(days=30)
    try:
        d_to = date.fromisoformat(q.get("day_to", ""))
    except ValueError:
        d_to = date.today()
    from datetime import time

    stmt = select(AuditLog).where(AuditLog.ts >= combine(d_from, time.min),
                                  AuditLog.ts < combine(d_to + timedelta(days=1), time.min))
    if q.get("username"):
        stmt = stmt.where(AuditLog.username == q["username"].strip().lower())
    if q.get("action"):
        stmt = stmt.where(AuditLog.action.contains(q["action"].strip()))
    with request.app.state.db.session() as s:
        rows = list(s.scalars(stmt.order_by(AuditLog.id.desc()).limit(2000)))
    return render(request, "audit.html", {"rows": rows, "day_from": d_from, "day_to": d_to, "q": dict(q)})
