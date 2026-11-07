"""Batched database writer for structured events (keeps DB I/O off the trading path)."""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import timedelta

from sqlalchemy import delete

from core.clock import now_ist
from core.events import Event
from storage.db import Database
from storage.models import EventRecord

log = logging.getLogger("algo.events.db")


class DbEventSink:
    def __init__(self, db: Database, flush_interval: float = 0.5, max_batch: int = 500) -> None:
        self.db = db
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self._buf: deque[Event] = deque(maxlen=100_000)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="event-db-writer", daemon=True)
        self._thread.start()

    def __call__(self, ev: Event) -> None:
        self._buf.append(ev)

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval):
            self.flush()
        self.flush()

    def flush(self) -> None:
        while self._buf:
            batch = []
            while self._buf and len(batch) < self.max_batch:
                batch.append(self._buf.popleft())
            try:
                with self.db.session() as s:
                    s.add_all([EventRecord(ts=e.ts, level=e.level.value, category=e.category.value,
                                           event_type=e.event_type, instance_id=e.instance_id,
                                           instance_name=e.instance_name, account_id=e.account_id,
                                           account_label=e.account_label, trade_id=e.trade_id,
                                           message=e.message, payload=e.payload) for e in batch])
            except Exception:
                log.exception("failed to write %d events", len(batch))
                return

    def purge_older_than(self, days: int) -> int:
        cutoff = now_ist() - timedelta(days=days)
        with self.db.session() as s:
            res = s.execute(delete(EventRecord).where(EventRecord.ts < cutoff))
            return res.rowcount or 0

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
