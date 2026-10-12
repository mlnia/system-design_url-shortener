"""Shared fixtures.

By default tests run against a throwaway SQLite file and the in-memory cache.
Set TEST_DATABASE_URL / TEST_REDIS_URL to run the same suite against Postgres/Redis.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.cache import create_cache
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.service import ShortenerService
from app.snowflake import SnowflakeGenerator


def unique_url() -> str:
    return f"https://example.com/articles/{uuid.uuid4().hex}?ref=test"


@pytest.fixture
def settings_factory(tmp_path):
    def factory(**overrides) -> Settings:
        values = {
            "database_url": os.getenv("TEST_DATABASE_URL")
            or f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
            "redis_url": os.getenv("TEST_REDIS_URL") or None,
            "cache_namespace": f"test-{uuid.uuid4().hex}",
            "rate_limit_requests": 1000,
            "machine_id": "1",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture
def client_factory(settings_factory):
    clients: list[TestClient] = []

    def factory(**overrides) -> TestClient:
        client = TestClient(create_app(settings_factory(**overrides)))
        client.__enter__()  # runs the lifespan (DB schema, cache, ID generator)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()


@pytest.fixture
async def service_factory(settings_factory):
    resources = []

    async def factory(**overrides) -> ShortenerService:
        settings = settings_factory(**overrides)
        db = Database(settings.database_url)
        await db.create_all()
        cache = create_cache(settings.redis_url)
        resources.append((db, cache))
        return ShortenerService(db, cache, SnowflakeGenerator(0, 1), settings)

    yield factory
    for db, cache in resources:
        await cache.close()
        await db.dispose()


@pytest.fixture
async def service(service_factory) -> ShortenerService:
    return await service_factory()
