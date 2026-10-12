"""Fixed window counter rate limiter (System Design Interview, ch. 4)."""

import math
import time
from dataclasses import dataclass

from app.cache import Cache


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_after),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.reset_after)
        return headers


class FixedWindowRateLimiter:
    def __init__(self, cache: Cache, *, limit: int, window_seconds: int, namespace: str) -> None:
        self._cache = cache
        self._limit = limit
        self._window = window_seconds
        self._namespace = namespace

    async def hit(self, identity: str) -> RateLimitResult:
        now = time.time()
        window_id = int(now // self._window)
        key = f"{self._namespace}:rl:{identity}:{window_id}"
        count = await self._cache.incr(key, ttl=self._window)
        reset_after = max(1, math.ceil(self._window - (now % self._window)))
        return RateLimitResult(
            allowed=count <= self._limit,
            limit=self._limit,
            remaining=max(0, self._limit - count),
            reset_after=reset_after,
        )
