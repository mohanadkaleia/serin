"""In-process fixed-window rate limiter (ENG-64 D6, §4.3).

Mechanism
---------
:class:`RateLimiter` is a fixed-window counter keyed by an arbitrary string:
``dict[key -> (window_start, count)]``. When a request arrives, the current
window is derived from the injected clock; if it advanced past the stored
window, the counter resets. Each check increments and reports whether the limit
was exceeded (the attempt still counts toward the window). ``Retry-After`` is the
seconds until the current window ends.

The clock is injectable (a monotonic ``now()`` by default) so tests advance time
without sleeping.

Single-worker honesty note
--------------------------
This state is **per-process in-memory**. The MVP runs **exactly one uvicorn
worker** (TDD §1/§11), so per-process == whole-server and this is correct as-is.
Horizontal scaling / multiple workers would need a shared store (e.g. Redis) —
explicitly out of MVP scope. The read-modify-write happens synchronously on the
event loop with no ``await`` between read and write, so no lock is needed; the
CPU-bound argon2 verify runs in a threadpool, separate from this state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request


@dataclass
class RateLimitResult:
    """Outcome of a single :meth:`RateLimiter.check`."""

    allowed: bool
    retry_after: int  # seconds until the current window ends (0 when allowed)


class RateLimiter:
    """Fixed-window counter, ``limit`` requests per ``window_seconds`` per key."""

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._now = now
        self._buckets: dict[str, tuple[float, int]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window

    def check(self, key: str) -> RateLimitResult:
        """Record one hit on ``key``; report whether it is within the limit.

        The hit always counts toward the window, so a rejected caller still
        extends its own denial for the remainder of the window.
        """
        now = self._now()
        window_start, count = self._buckets.get(key, (now, 0))
        if now - window_start >= self._window:
            # Window elapsed → start a fresh one at the current instant.
            window_start, count = now, 0
        count += 1
        self._buckets[key] = (window_start, count)
        if count > self._limit:
            retry_after = max(1, int(window_start + self._window - now) + 1)
            return RateLimitResult(allowed=False, retry_after=retry_after)
        return RateLimitResult(allowed=True, retry_after=0)


def client_ip(request: Request, *, trust_proxy: bool) -> str:
    """Best-effort client IP for per-IP limiting (D6).

    ``trust_proxy`` off (default) → the socket peer (``request.client.host``).
    On → the leftmost ``X-Forwarded-For`` hop. Behind a reverse proxy the
    operator must set XFF and enable ``trust_proxy`` or all callers share the
    proxy's IP bucket.
    """
    if trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    if request.client is not None:
        return request.client.host
    return "unknown"
