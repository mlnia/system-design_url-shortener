"""Core URL shortening logic: code generation, deduplication, caching and lookups."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import base62
from app.cache import Cache
from app.config import Settings
from app.db import Database
from app.models import URL
from app.snowflake import SnowflakeGenerator

logger = logging.getLogger(__name__)

# Paths served by the app itself; a short code must never shadow them.
RESERVED_CODES = frozenset(
    {"api", "docs", "redoc", "openapi.json", "static", "healthz", "favicon.ico", "robots.txt"}
)
# The book's collision resolution: append a predefined string and hash again.
HASH_COLLISION_SUFFIX = "#url-shortener#"
MAX_INSERT_ATTEMPTS = 8
_NEGATIVE_CACHE_MARKER = ""


class ShortenerError(Exception):
    pass


class AliasTakenError(ShortenerError):
    pass


class ReservedAliasError(ShortenerError):
    pass


class URLNotFoundError(ShortenerError):
    pass


class URLExpiredError(ShortenerError):
    pass


class CodeGenerationError(ShortenerError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo on the way back; everything we store is UTC.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def url_fingerprint(long_url: str) -> str:
    return hashlib.sha256(long_url.encode()).hexdigest()


@dataclass(frozen=True)
class ShortenResult:
    url: URL
    created: bool


class ShortenerService:
    def __init__(
        self,
        db: Database,
        cache: Cache,
        id_generator: SnowflakeGenerator,
        settings: Settings,
    ) -> None:
        self._db = db
        self._cache = cache
        self._ids = id_generator
        self._settings = settings

    def _cache_key(self, short_code: str) -> str:
        return f"{self._settings.cache_namespace}:url:{short_code}"

    async def shorten(
        self,
        long_url: str,
        *,
        custom_alias: str | None = None,
        expires_at: datetime | None = None,
    ) -> ShortenResult:
        expires_at = as_utc(expires_at)
        fingerprint = url_fingerprint(long_url)

        async with self._db.session() as session:
            if custom_alias:
                if custom_alias.lower() in RESERVED_CODES:
                    raise ReservedAliasError(custom_alias)
                url = self._new_url(custom_alias, long_url, fingerprint, expires_at, is_custom=True)
                if not await self._try_insert(session, url):
                    raise AliasTakenError(custom_alias)
                await self._cache.delete(self._cache_key(custom_alias))
                return ShortenResult(url, created=True)

            # The same long URL always maps to the same short URL (book: "if longURL is
            # in DB, return the existing shortURL"). Expiring links are always fresh.
            if expires_at is None:
                existing = await self._find_existing(session, long_url, fingerprint)
                if existing is not None:
                    return ShortenResult(existing, created=False)

            for attempt in range(MAX_INSERT_ATTEMPTS):
                url_id = self._ids.next_id()
                if self._settings.shortener_strategy == "base62":
                    short_code = base62.encode(url_id)
                else:
                    short_code = self._hash_code(long_url, attempt)
                if short_code.lower() in RESERVED_CODES:
                    continue
                url = self._new_url(short_code, long_url, fingerprint, expires_at, url_id=url_id)
                # The unique constraint on short_code is the collision check: instead of
                # "SELECT then INSERT" (racy across replicas) we insert and retry on conflict.
                if await self._try_insert(session, url):
                    await self._cache.delete(self._cache_key(short_code))
                    return ShortenResult(url, created=True)
                logger.info("short code collision on %r (attempt %d)", short_code, attempt + 1)

        raise CodeGenerationError("could not generate a unique short code")

    async def resolve(self, short_code: str) -> str:
        """Return the long URL for ``short_code``, serving from cache when possible."""
        key = self._cache_key(short_code)
        cached = await self._cache.get(key)
        if cached is not None:
            if cached == _NEGATIVE_CACHE_MARKER:
                raise URLNotFoundError(short_code)
            entry = json.loads(cached)
            if entry["e"] is not None and entry["e"] <= time.time():
                raise URLExpiredError(short_code)
            return entry["u"]

        async with self._db.session() as session:
            url = await session.scalar(select(URL).where(URL.short_code == short_code))

        if url is None:
            # Short-lived negative entry protects the DB from floods of unknown codes.
            await self._cache.set(key, _NEGATIVE_CACHE_MARKER, self._settings.negative_cache_ttl_seconds)
            raise URLNotFoundError(short_code)

        expires_at = as_utc(url.expires_at)
        ttl = self._settings.cache_ttl_seconds
        if expires_at is not None:
            remaining = (expires_at - utcnow()).total_seconds()
            if remaining <= 0:
                raise URLExpiredError(short_code)
            ttl = max(1, min(ttl, int(remaining)))

        entry = {"u": url.long_url, "e": expires_at.timestamp() if expires_at else None}
        await self._cache.set(key, json.dumps(entry), ttl)
        return url.long_url

    async def get(self, short_code: str) -> URL:
        async with self._db.session() as session:
            url = await session.scalar(select(URL).where(URL.short_code == short_code))
        if url is None:
            raise URLNotFoundError(short_code)
        return url

    async def record_click(self, short_code: str) -> None:
        try:
            async with self._db.session() as session:
                await session.execute(
                    update(URL).where(URL.short_code == short_code).values(clicks=URL.clicks + 1)
                )
                await session.commit()
        except Exception:
            logger.warning("failed to record click for %r", short_code, exc_info=True)

    def _hash_code(self, long_url: str, attempt: int) -> str:
        data = long_url if attempt == 0 else f"{long_url}{HASH_COLLISION_SUFFIX}{attempt}"
        digest = hashlib.md5(data.encode(), usedforsecurity=False).digest()
        length = self._settings.hash_code_length
        # Low-order digits are uniformly distributed; the leading digit is not.
        return base62.encode(int.from_bytes(digest, "big")).rjust(length, "0")[-length:]

    def _new_url(
        self,
        short_code: str,
        long_url: str,
        fingerprint: str,
        expires_at: datetime | None,
        *,
        url_id: int | None = None,
        is_custom: bool = False,
    ) -> URL:
        return URL(
            id=url_id if url_id is not None else self._ids.next_id(),
            short_code=short_code,
            long_url=long_url,
            long_url_hash=fingerprint,
            is_custom=is_custom,
            clicks=0,
            created_at=utcnow(),
            expires_at=expires_at,
        )

    @staticmethod
    async def _find_existing(session: AsyncSession, long_url: str, fingerprint: str) -> URL | None:
        rows = await session.scalars(
            select(URL)
            .where(
                URL.long_url_hash == fingerprint,
                URL.is_custom.is_(False),
                URL.expires_at.is_(None),
            )
            .limit(5)
        )
        return next((row for row in rows if row.long_url == long_url), None)

    @staticmethod
    async def _try_insert(session: AsyncSession, url: URL) -> bool:
        session.add(url)
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False
