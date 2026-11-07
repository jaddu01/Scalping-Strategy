"""Live WebSocket feed: state snapshots every second plus every event as it happens."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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
    q = platform.hub.connect()
    try:
        await ws.send_text(json.dumps({"type": "recent", "data": list(platform.hub.recent)[-300:]}, default=str))
        await ws.send_text(json.dumps({"type": "state", "data": platform.snapshot()}, default=str))
        last_check = asyncio.get_running_loop().time()
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                await ws.send_text('{"type":"ping"}')
            now = asyncio.get_running_loop().time()
            if now - last_check > 30:  # re-validate the session (idle timeout, logout elsewhere)
                last_check = now
                if app.state.sessions.get(ws.cookies.get(SESSION_COOKIE), touch=False) is None:
                    await ws.close(code=4401)
                    return
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        platform.hub.disconnect(q)
