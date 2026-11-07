"""Fan-out of live updates (events, state snapshots, banners) to connected browser websockets."""
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from core.events import Event


class UiHub:
    def __init__(self, recent: int = 500) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self.recent: deque[dict[str, Any]] = deque(maxlen=recent)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def connect(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def disconnect(self, q: asyncio.Queue[str]) -> None:
        self._clients.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        if not self._clients:
            return
        msg = json.dumps({"type": kind, "data": data}, default=str)
        for q in list(self._clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # slow client: drop its oldest message rather than block the engine
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass

    def event_sink(self, ev: Event) -> None:
        d = ev.to_dict()
        self.recent.append(d)
        self.publish("event", d)
