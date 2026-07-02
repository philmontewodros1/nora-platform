"""Minimal in-memory rate limiter (no extra deps, fine for a single-process
free-tier pilot). For multi-worker production, swap for Redis-backed limits."""
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: int):
        self.max = max_calls
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        return True


# max 5 orders per minute per customer phone — stops spam / accidental loops
order_limiter = RateLimiter(max_calls=5, window_seconds=60)
