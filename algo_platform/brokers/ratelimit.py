"""Async rate limiting and retry-with-backoff helpers for broker API calls."""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class RateLimiter:
    """Sliding-window limiter: at most `rate` calls per `per` seconds for each bucket."""

    def __init__(self, limits: dict[str, tuple[int, float]], default: tuple[int, float] = (5, 1.0)):
        self._limits = limits
        self._default = default
        self._calls: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, bucket: str) -> None:
        rate, per = self._limits.get(bucket, self._default)
        lock = self._locks.setdefault(bucket, asyncio.Lock())
        calls = self._calls.setdefault(bucket, deque())
        async with lock:
            while True:
                now = time.monotonic()
                while calls and now - calls[0] >= per:
                    calls.popleft()
                if len(calls) < rate:
                    calls.append(now)
                    return
                await asyncio.sleep(per - (now - calls[0]) + 0.001)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    is_retryable: Callable[[BaseException], bool] = lambda e: True,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call fn, retrying retryable errors with exponential backoff and jitter."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) * (0.8 + 0.4 * random.random())
            if on_retry:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
