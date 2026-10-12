from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from pydantic.alias_generators import to_camel

SHORT_CODE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
ALIAS_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"


class CamelModel(BaseModel):
    # The book's API uses camelCase (longUrl / shortUrl); snake_case is accepted too.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ShortenRequest(CamelModel):
    long_url: HttpUrl
    custom_alias: str | None = Field(default=None, pattern=ALIAS_PATTERN)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _must_be_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value <= datetime.now(timezone.utc):
            raise ValueError("expiresAt must be in the future")
        return value


class ShortenResponse(CamelModel):
    short_url: str
    short_code: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None = None


class URLStats(ShortenResponse):
    clicks: int


class Health(BaseModel):
    status: str
    database: bool
    cache: bool
