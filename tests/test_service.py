import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.models import URL
from app.service import (
    AliasTakenError,
    ReservedAliasError,
    URLExpiredError,
    URLNotFoundError,
    utcnow,
)
from tests.conftest import unique_url


async def test_shorten_and_resolve(service):
    long_url = unique_url()
    result = await service.shorten(long_url)
    assert result.created
    assert await service.resolve(result.url.short_code) == long_url
    # Second lookup is served from cache.
    assert await service.resolve(result.url.short_code) == long_url


async def test_same_long_url_returns_same_code(service):
    long_url = unique_url()
    first = await service.shorten(long_url)
    second = await service.shorten(long_url)
    assert not second.created
    assert second.url.short_code == first.url.short_code


async def test_expiring_links_are_not_deduplicated(service):
    long_url = unique_url()
    permanent = await service.shorten(long_url)
    expiring = await service.shorten(long_url, expires_at=utcnow() + timedelta(days=1))
    assert expiring.created
    assert expiring.url.short_code != permanent.url.short_code


async def test_custom_alias(service):
    alias = f"a-{uuid.uuid4().hex[:8]}"
    result = await service.shorten(unique_url(), custom_alias=alias)
    assert result.url.short_code == alias
    with pytest.raises(AliasTakenError):
        await service.shorten(unique_url(), custom_alias=alias)


async def test_reserved_alias(service):
    with pytest.raises(ReservedAliasError):
        await service.shorten(unique_url(), custom_alias="docs")


async def test_unknown_code_is_negatively_cached_then_cleared_on_create(service):
    alias = f"late-{uuid.uuid4().hex[:8]}"
    with pytest.raises(URLNotFoundError):
        await service.resolve(alias)
    long_url = unique_url()
    await service.shorten(long_url, custom_alias=alias)
    assert await service.resolve(alias) == long_url


async def test_expired_link(service):
    result = await service.shorten(unique_url(), expires_at=utcnow() + timedelta(hours=1))
    async with service._db.session() as session:
        await session.execute(
            update(URL).where(URL.id == result.url.id).values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()
    with pytest.raises(URLExpiredError):
        await service.resolve(result.url.short_code)


async def test_hash_strategy_produces_fixed_length_codes(service_factory):
    service = await service_factory(shortener_strategy="hash", hash_code_length=7)
    long_url = unique_url()
    result = await service.shorten(long_url)
    assert len(result.url.short_code) == 7
    assert result.url.short_code == service._hash_code(long_url, 0)


async def test_hash_collision_is_resolved(service_factory, monkeypatch):
    service = await service_factory(shortener_strategy="hash")
    first = await service.shorten(unique_url())

    real_hash = service._hash_code

    def colliding_hash(long_url, attempt):
        # Force the first attempt to collide with an existing code.
        return first.url.short_code if attempt == 0 else real_hash(long_url, attempt)

    monkeypatch.setattr(service, "_hash_code", colliding_hash)
    long_url = unique_url()
    second = await service.shorten(long_url)
    assert second.created
    assert second.url.short_code == real_hash(long_url, 1)
    assert await service.resolve(second.url.short_code) == long_url


async def test_record_click(service):
    result = await service.shorten(unique_url())
    await service.record_click(result.url.short_code)
    await service.record_click(result.url.short_code)
    assert (await service.get(result.url.short_code)).clicks == 2
