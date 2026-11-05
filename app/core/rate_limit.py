"""Sliding-window rate limiting keyed by authenticated user (or client IP when anonymous).

This is per-process state. On Cloud Run with several instances the effective limit is
``limit * instances``; move the counters to Redis before relying on it as an abuse control.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_seconds - (now - q[0])))
                raise HTTPException(
                    status_code=429, detail="Too many requests.", headers={"Retry-After": str(retry)}
                )
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
