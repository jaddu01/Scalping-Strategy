from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import SessionInfo, current_user
from app.web.render import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    p = request.app.state.platform
    snap = p.snapshot() if p is not None else None
    return render(request, "dashboard.html", {"snap": snap, "snap_json": json.dumps(snap, default=str)})
