"""Telegram alerts. Acts as an EventBus sink: events matching the configured categories/level
(or listed in 'always send') are queued and sent without blocking the trading path."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

import httpx

from core.events import Event, Level, format_line
from core.settings_schema import Notifications

log = logging.getLogger("algo.telegram")
API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(token: str, chat_id: str, text: str, timeout: float = 10.0) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "bot token and chat id are required"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(API.format(token=token), json={"chat_id": chat_id, "text": text[:4000],
                                                                 "disable_web_page_preview": True})
        if r.status_code == 200 and r.json().get("ok"):
            return True, "sent"
        return False, f"telegram error {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, f"telegram request failed: {exc}"


class TelegramNotifier:
    def __init__(self, settings: Callable[[], Notifications], token: Callable[[], str]) -> None:
        self.settings = settings
        self.token = token
        self._queue: asyncio.Queue[str] | None = None
        self._task: asyncio.Task | None = None

    def wants(self, ev: Event) -> bool:
        n = self.settings()
        if not n.telegram_enabled or not n.telegram_chat_id:
            return False
        if ev.event_type in n.always_send or f"{ev.category.value}.{ev.event_type}" in n.always_send:
            return True
        return ev.category.value in n.categories and ev.level.rank >= Level(n.min_level).rank

    def __call__(self, ev: Event) -> None:
        if self._queue is None or not self.wants(ev):
            return
        icon = {"CRITICAL": "🚨", "ERROR": "❗", "WARNING": "⚠️"}.get(ev.level.value, "ℹ️")
        try:
            self._queue.put_nowait(f"{icon} {format_line(ev)}")
        except asyncio.QueueFull:
            pass

    def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=500)
        self._task = asyncio.get_running_loop().create_task(self._run(), name="telegram")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            text = await self._queue.get()
            ok, msg = await send_telegram(self.token(), self.settings().telegram_chat_id, text)
            if not ok:
                log.warning("telegram send failed: %s", msg)
            await asyncio.sleep(1.1)  # stay under Telegram's per-chat rate limit
