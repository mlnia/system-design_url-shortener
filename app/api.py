import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.config import Settings
from app.models import URL
from app.rate_limiter import FixedWindowRateLimiter
from app.schemas import SHORT_CODE_PATTERN, Health, ShortenRequest, ShortenResponse, URLStats
from app.service import (
    AliasTakenError,
    CodeGenerationError,
    ReservedAliasError,
    ShortenerService,
    URLExpiredError,
    URLNotFoundError,
    as_utc,
)

STATIC_DIR = Path(__file__).parent / "static"
_SHORT_CODE_RE = re.compile(SHORT_CODE_PATTERN)

router = APIRouter()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_service(request: Request) -> ShortenerService:
    return request.app.state.service


async def enforce_rate_limit(request: Request, response: Response) -> None:
    limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
    client_ip = request.client.host if request.client else "unknown"
    result = await limiter.hit(client_ip)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests, slow down.",
            headers=result.headers(),
        )
    response.headers.update(result.headers())


def _public_base(request: Request, settings: Settings) -> str:
    return (settings.base_url or str(request.base_url)).rstrip("/")


def _to_response(url: URL, request: Request, settings: Settings, model=ShortenResponse):
    data = {
        "short_url": f"{_public_base(request, settings)}/{url.short_code}",
        "short_code": url.short_code,
        "long_url": url.long_url,
        "created_at": as_utc(url.created_at),
        "expires_at": as_utc(url.expires_at),
    }
    if model is URLStats:
        data["clicks"] = url.clicks
    return model(**data)


def _is_own_domain(long_url: str, request: Request, settings: Settings) -> bool:
    target = (urlsplit(long_url).hostname or "").lower()
    own_hosts = {(request.url.hostname or "").lower()}
    if settings.base_url:
        own_hosts.add((urlsplit(settings.base_url).hostname or "").lower())
    return target in own_hosts


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/healthz", response_model=Health, tags=["ops"])
async def healthz(request: Request) -> JSONResponse:
    db_ok = await request.app.state.db.ping()
    cache_ok = await request.app.state.cache.ping()
    body = Health(status="ok" if db_ok else "unavailable", database=db_ok, cache=cache_ok)
    # A cache outage only degrades performance, so it doesn't fail the health check.
    return JSONResponse(body.model_dump(), status_code=200 if db_ok else 503)


@router.post(
    "/api/v1/data/shorten",
    response_model=ShortenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_rate_limit)],
    tags=["urls"],
    summary="Create a short URL",
    responses={200: {"description": "The long URL was already shortened; existing link returned"}},
)
async def shorten(
    payload: ShortenRequest,
    request: Request,
    response: Response,
    service: ShortenerService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> ShortenResponse:
    long_url = str(payload.long_url)
    if len(long_url) > settings.max_url_length:
        raise HTTPException(
            status_code=422,
            detail=f"longUrl must be at most {settings.max_url_length} characters",
        )
    if _is_own_domain(long_url, request, settings):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot shorten our own links")

    try:
        result = await service.shorten(
            long_url, custom_alias=payload.custom_alias, expires_at=payload.expires_at
        )
    except ReservedAliasError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This alias is reserved")
    except AliasTakenError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This alias is already taken")
    except CodeGenerationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not allocate a short code"
        )

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _to_response(result.url, request, settings)


@router.get("/api/v1/urls/{short_code}", response_model=URLStats, tags=["urls"], summary="Link details and click count")
async def stats(
    short_code: str,
    request: Request,
    service: ShortenerService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> URLStats:
    try:
        url = await service.get(short_code)
    except URLNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found")
    return _to_response(url, request, settings, model=URLStats)


# Must stay the last route: it matches any single path segment.
@router.get(
    "/{short_code}",
    tags=["redirect"],
    summary="Redirect to the long URL",
    response_class=RedirectResponse,
    responses={
        301: {"description": "Permanent redirect"},
        302: {"description": "Temporary redirect"},
        404: {"description": "Unknown short code"},
        410: {"description": "Link expired"},
    },
)
async def redirect(
    short_code: str,
    background_tasks: BackgroundTasks,
    service: ShortenerService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if not _SHORT_CODE_RE.match(short_code):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found")
    try:
        long_url = await service.resolve(short_code)
    except URLNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found")
    except URLExpiredError:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This short URL has expired")

    if settings.track_clicks:
        background_tasks.add_task(service.record_click, short_code)
    return RedirectResponse(long_url, status_code=settings.redirect_status_code)
