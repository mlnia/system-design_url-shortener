from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, read from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "URL Shortener"
    # Public origin used to build short links, e.g. https://sho.rt.
    # When unset, the origin of the incoming request is used.
    base_url: str | None = None

    database_url: str = "sqlite+aiosqlite:///./shortener.db"
    db_echo: bool = False

    # When unset, an in-process LRU cache is used instead of Redis.
    redis_url: str | None = None
    cache_namespace: str = "shortener"
    cache_ttl_seconds: int = 24 * 3600
    negative_cache_ttl_seconds: int = 60

    # "base62": unique Snowflake ID converted to base62 (the approach the book picks).
    # "hash":   hash the long URL, take N chars, resolve collisions by re-hashing.
    shortener_strategy: Literal["base62", "hash"] = "base62"
    hash_code_length: int = 7

    # 301 = permanent (browser caches it, less server load).
    # 302 = temporary (every click hits us, so analytics are accurate).
    redirect_status_code: int = 302
    track_clicks: bool = True

    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60

    # Snowflake worker identity. "auto" leases a unique id from Redis at startup.
    datacenter_id: int = 0
    machine_id: str = "auto"

    max_url_length: int = 2048

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        # Hosting providers (Render, Heroku, Fly...) hand out postgres:// URLs.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    @field_validator("base_url", "redis_url")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("redirect_status_code")
    @classmethod
    def _check_redirect_status(cls, value: int) -> int:
        if value not in (301, 302):
            raise ValueError("redirect_status_code must be 301 or 302")
        return value

    @field_validator("datacenter_id")
    @classmethod
    def _check_datacenter(cls, value: int) -> int:
        if not 0 <= value <= 31:
            raise ValueError("datacenter_id must be between 0 and 31")
        return value

    @field_validator("machine_id")
    @classmethod
    def _check_machine(cls, value: str) -> str:
        if value == "auto":
            return value
        if not value.isdigit() or not 0 <= int(value) <= 31:
            raise ValueError('machine_id must be "auto" or between 0 and 31')
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
