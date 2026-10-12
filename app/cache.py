"""Cache backends: Redis in production, an in-process LRU for local runs and tests."""

import logging
import time
from collections import OrderedDict
from typing import Protocol

logger = logging.getLogger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl: int | None = None) -> int:
        """Increment a counter; ``ttl`` is applied only when the key is created."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryCache:
    def __init__(self, max_items: int = 10_000) -> None:
        self._max_items = max_items
        self._data: OrderedDict[str, tuple[str, float | None]] = OrderedDict()

    def _live_item(self, key: str) -> tuple[str, float | None] | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at = item[1]
        if expires_at is not None and expires_at <= time.monotonic():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return item

    def _store(self, key: str, value: str, expires_at: float | None) -> None:
        self._data[key] = (value, expires_at)
        self._data.move_to_end(key)
        while len(self._data) > self._max_items:
            self._data.popitem(last=False)

    async def get(self, key: str) -> str | None:
        item = self._live_item(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._store(key, value, time.monotonic() + ttl if ttl else None)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int | None = None) -> int:
        item = self._live_item(key)
        if item is None:
            value, expires_at = 1, (time.monotonic() + ttl if ttl else None)
        else:
            value, expires_at = int(item[0]) + 1, item[1]
        self._store(key, str(value), expires_at)
        return value

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    """Redis-backed cache. Errors are logged and swallowed so that a cache outage
    degrades to "every request hits the database" instead of taking the service down."""

    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        self._redis = Redis.from_url(url, decode_responses=True, socket_timeout=2)

    async def get(self, key: str) -> str | None:
        from redis.exceptions import RedisError

        try:
            return await self._redis.get(key)
        except RedisError:
            logger.warning("redis GET failed", exc_info=True)
            return None

    async def set(self, key: str, value: str, ttl: int) -> None:
        from redis.exceptions import RedisError

        try:
            await self._redis.set(key, value, ex=ttl or None)
        except RedisError:
            logger.warning("redis SET failed", exc_info=True)

    async def delete(self, key: str) -> None:
        from redis.exceptions import RedisError

        try:
            await self._redis.delete(key)
        except RedisError:
            logger.warning("redis DEL failed", exc_info=True)

    async def incr(self, key: str, ttl: int | None = None) -> int:
        from redis.exceptions import RedisError

        try:
            value = await self._redis.incr(key)
            if ttl and value == 1:
                await self._redis.expire(key, ttl)
            return value
        except RedisError:
            # Fail open: callers treat 0 as "unknown" (e.g. the rate limiter allows the request).
            logger.warning("redis INCR failed", exc_info=True)
            return 0

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def create_cache(redis_url: str | None) -> Cache:
    if redis_url:
        return RedisCache(redis_url)
    logger.info("REDIS_URL not set, using in-memory cache")
    return MemoryCache()
