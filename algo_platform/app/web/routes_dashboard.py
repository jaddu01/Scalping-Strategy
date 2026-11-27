from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.access import is_admin, owner_choices, owner_filter, request_scope, scope_snapshot
from app.auth.deps import SessionInfo, current_user
from app.web.render import render, safe_json
from core.clock import now_ist

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    snap = scope_snapshot(p.snapshot(), request_scope(request, user)) if p is not None else None
    today = (p.clock.now() if p is not None else now_ist()).date().isoformat()
    return render(request, "dashboard.html", {"snap": snap, "snap_json": safe_json(snap), "today": today,
                                              "owner": owner_filter(request, user)[0],
                                              "owners": owner_choices(request.app.state.db) if is_admin(user) else []})
