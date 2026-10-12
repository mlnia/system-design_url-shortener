import logging
import os
import socket
import zlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.cache import Cache, RedisCache, create_cache
from app.config import Settings, get_settings
from app.db import Database
from app.rate_limiter import FixedWindowRateLimiter
from app.service import ShortenerService
from app.snowflake import MAX_MACHINE_ID, SnowflakeGenerator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("app")


async def resolve_worker_ids(settings: Settings, cache: Cache) -> tuple[int, int]:
    """Pick the (datacenter_id, machine_id) pair for this process's Snowflake generator.

    Every running instance needs a distinct pair or IDs can collide. With Redis we
    hand out sequential worker numbers (0..1023); otherwise derive one from host+pid.
    """
    if settings.machine_id != "auto":
        return settings.datacenter_id, int(settings.machine_id)

    if isinstance(cache, RedisCache):
        ticket = await cache.incr(f"{settings.cache_namespace}:snowflake:worker-seq")
        if ticket:
            worker = (ticket - 1) % 1024
            return worker >> 5, worker & MAX_MACHINE_ID

    worker = zlib.crc32(f"{socket.gethostname()}:{os.getpid()}".encode()) % (MAX_MACHINE_ID + 1)
    return settings.datacenter_id, worker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url, echo=settings.db_echo)
        await db.create_all()
        cache = create_cache(settings.redis_url)
        datacenter_id, machine_id = await resolve_worker_ids(settings, cache)

        app.state.settings = settings
        app.state.db = db
        app.state.cache = cache
        app.state.service = ShortenerService(
            db, cache, SnowflakeGenerator(datacenter_id, machine_id), settings
        )
        app.state.rate_limiter = FixedWindowRateLimiter(
            cache,
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            namespace=settings.cache_namespace,
        )
        logger.info(
            "started: strategy=%s redirect=%d cache=%s snowflake=(dc=%d, machine=%d)",
            settings.shortener_strategy,
            settings.redirect_status_code,
            type(cache).__name__,
            datacenter_id,
            machine_id,
        )
        try:
            yield
        finally:
            await cache.close()
            await db.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="URL shortener from *System Design Interview* (Alex Xu), chapter 8.",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
