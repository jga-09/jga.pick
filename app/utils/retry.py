"""Async retry with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.errors import KalshiRateLimitError, KalshiTransientError

T = TypeVar("T")
log = logging.getLogger(__name__)

RETRYABLE: tuple[type[BaseException], ...] = (KalshiTransientError, KalshiRateLimitError)


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Full-jitter exponential backoff: random in [0, min(cap, base*2^attempt)]."""
    return random.uniform(0, min(cap, base * (2**attempt))) + base / 2


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base: float = 0.5,
    cap: float = 15.0,
    retry_on: tuple[type[BaseException], ...] = RETRYABLE,
    label: str = "",
) -> T:
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except retry_on as exc:
            last = exc
            if attempt == attempts - 1:
                break
            delay = backoff_delay(attempt, base, cap)
            if isinstance(exc, KalshiRateLimitError) and exc.retry_after:
                delay = max(delay, exc.retry_after)
            log.warning("RETRY op=%s attempt=%d delay=%.2f error=%s", label, attempt + 1, delay, exc)
            await asyncio.sleep(delay)
    assert last is not None
    raise last


class AsyncRateLimiter:
    """Simple token bucket for request pacing (shared across tasks)."""

    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            while True:
                now = loop.time()
                if self._last is None:
                    self._last = now
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)
