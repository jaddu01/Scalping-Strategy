"""Live WebSocket feed: state snapshots every second plus every event as it happens."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth.access import scope_for, scope_snapshot
from app.auth.deps import SESSION_COOKIE

router = APIRouter()


@router.websocket("/ws")
async def live(ws: WebSocket) -> None:
    app = ws.app
    info = app.state.sessions.get(ws.cookies.get(SESSION_COOKIE))
    origin = ws.headers.get("origin")
    host = ws.headers.get("host", "")
    if info is None or info.pending_2fa or (origin and urlparse(origin).netloc != host):
        await ws.close(code=4401)
        return
    await ws.accept()
    platform = app.state.platform
    if platform is None:
        await ws.send_text(json.dumps({"type": "error", "data": "trading engine is not running"}))
        await ws.close()
        return
    owner = ws.query_params.get("owner")
    scope = scope_for(app.state.db, info, owner)  # each viewer gets only their own accounts / strategies
    q = platform.hub.connect()
    try:
        recent = [e for e in list(platform.hub.recent) if scope.event(e)][-300:]
        await ws.send_text(json.dumps({"type": "recent", "data": recent}, default=str))
        await ws.send_text(json.dumps({"type": "state", "data": scope_snapshot(platform.snapshot(), scope)},
                                      default=str))
        last_check = asyncio.get_running_loop().time()
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                if not scope.everything:
                    m = json.loads(msg)
                    if m.get("type") == "state":
                        msg = json.dumps({"type": "state", "data": scope_snapshot(m["data"], scope)}, default=str)
                    elif m.get("type") == "event" and not scope.event(m.get("data") or {}):
                        continue
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                await ws.send_text('{"type":"ping"}')
            now = asyncio.get_running_loop().time()
            if now - last_check > 30:  # re-validate the session (idle timeout, logout elsewhere)
                last_check = now
                info = app.state.sessions.get(ws.cookies.get(SESSION_COOKIE), touch=False)
                if info is None:
                    await ws.close(code=4401)
                    return
                scope = scope_for(app.state.db, info, owner)  # picks up new / deleted runs and accounts
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        platform.hub.disconnect(q)
