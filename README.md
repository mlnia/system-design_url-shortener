# URL Shortener

A working, deployable implementation of **Chapter 8: Design a URL Shortener** from
*System Design Interview – An Insider's Guide* by Alex Xu.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Redis · nginx · Docker

## Architecture

```mermaid
flowchart LR
    C[Client] --> LB[nginx<br/>load balancer]
    LB --> W1[FastAPI #1]
    LB --> W2[FastAPI #2]
    W1 & W2 --> R[(Redis<br/>cache + rate limit<br/>+ worker id)]
    W1 & W2 --> DB[(PostgreSQL<br/>urls)]
```

How the book's concepts map to the code:

| Concept from the book | Implementation |
|---|---|
| `POST api/v1/data/shorten` / `GET api/v1/shortUrl` | `app/api.py`: `POST /api/v1/data/shorten`, `GET /{shortCode}` |
| 301 vs 302 redirect | `REDIRECT_STATUS_CODE` (defaults to 302 so click analytics stay accurate) |
| Unique ID generator (Chapter 7, Snowflake) | `app/snowflake.py`: 41-bit timestamp + 5-bit datacenter + 5-bit machine + 12-bit sequence |
| Base 62 conversion | `app/base62.py` |
| Hash + collision resolution | `SHORTENER_STRATEGY=hash`: MD5 → base62 → 7 characters; on a collision, a predefined string is appended and the URL is hashed again (`app/service.py`) |
| "If the longURL is already in the DB, return the existing shortURL" | Indexed lookup on a SHA-256 fingerprint; the repeat request returns `200` |
| Cache (read-heavy, 10:1) | `<shortCode> → longURL` in Redis with a TTL, plus a short-lived negative cache for unknown codes |
| Rate limiter (Chapter 4) | `app/rate_limiter.py`: fixed window counter per IP, `429` + `Retry-After` |
| Stateless web tier / horizontal scaling | 2 replicas behind nginx in Compose; each replica gets a unique Snowflake worker ID from Redis |
| Database scaling / availability | PostgreSQL; schema creation is guarded by an advisory lock so concurrent replicas are safe |

Additions that are not in the book but a real service needs: custom aliases (`customAlias`),
expiration (`expiresAt` → `410 Gone` once expired), a click counter and stats endpoint,
blocking links to our own domain, `/healthz`, a simple web UI, and graceful degradation to
the database if Redis goes down.

## Running

### 1) Plain Python (no Docker, fastest)

Runs on SQLite + an in-memory cache, with no extra services.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

→ http://localhost:8000 (UI), http://localhost:8000/docs (Swagger)

### 2) Full stack with Docker Compose

nginx + 2 app replicas + PostgreSQL + Redis:

```bash
docker compose up --build
```

→ http://localhost:8080

To change the strategy or redirect code:
`SHORTENER_STRATEGY=hash REDIRECT_STATUS_CODE=301 docker compose up --build`

### 3) Local Python + Postgres/Redis in Docker

```bash
docker compose up -d postgres redis   # add `ports` to those services in compose to expose them
```
Then copy `.env.example` to `.env` and uncomment the `DATABASE_URL` / `REDIS_URL` lines.

## API

```bash
# Shorten
curl -X POST http://localhost:8000/api/v1/data/shorten \
  -H "Content-Type: application/json" \
  -d '{"longUrl": "https://en.wikipedia.org/wiki/Systems_design"}'
# 201 {"shortUrl":"http://localhost:8000/7CB982fuFO","shortCode":"7CB982fuFO",...}

# Custom alias + expiration
curl -X POST http://localhost:8000/api/v1/data/shorten \
  -H "Content-Type: application/json" \
  -d '{"longUrl": "https://github.com", "customAlias": "gh", "expiresAt": "2027-01-01T00:00:00Z"}'

# Redirect
curl -i http://localhost:8000/7CB982fuFO       # 302 Location: https://en.wikipedia.org/...

# Stats
curl http://localhost:8000/api/v1/urls/7CB982fuFO   # {..., "clicks": 2}
```

| Status | Meaning |
|---|---|
| `201` | New short link created |
| `200` | This long URL was already shortened; the existing link is returned |
| `301/302` | Redirect |
| `400` | Reserved alias, or a link to our own domain |
| `404` / `410` | Unknown code / expired link |
| `409` | Alias already taken |
| `422` | Invalid URL / alias / date |
| `429` | Rate limit exceeded |

## Configuration

All settings come from environment variables (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./shortener.db` | `postgres://…` is accepted and converted to asyncpg automatically |
| `REDIS_URL` | empty | When empty, an in-memory LRU cache is used |
| `BASE_URL` | empty | Public origin for short links; when empty, the request's host is used |
| `SHORTENER_STRATEGY` | `base62` | `base62` or `hash` |
| `HASH_CODE_LENGTH` | `7` | Code length for the `hash` strategy |
| `REDIRECT_STATUS_CODE` | `302` | `301` or `302` |
| `TRACK_CLICKS` | `true` | Increment the click counter on redirect |
| `CACHE_TTL_SECONDS` | `86400` | Cache TTL |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `30` / `60` | Limit for the shorten endpoint (per IP) |
| `MACHINE_ID` / `DATACENTER_ID` | `auto` / `0` | Snowflake worker identity |
| `WEB_CONCURRENCY` | `1` | Uvicorn workers per container |

## Tests

```bash
pytest -q
```

By default the tests use SQLite + the in-memory cache. The same suite also runs against a
real Postgres/Redis:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://shortener:shortener@localhost:5432/shortener \
TEST_REDIS_URL=redis://localhost:6379/0 pytest -q
```

GitHub Actions (`.github/workflows/ci.yml`) runs both modes plus a Docker image build.

## Deployment

**Render (one click):** push the repo to GitHub → Render Dashboard → *New → Blueprint* →
select the repo. `render.yaml` creates and wires up the web service (Docker), PostgreSQL and a
Redis-compatible Key Value store. Set `BASE_URL` once you add a custom domain.

**Any Docker environment** (Fly.io, Railway, ECS, Cloud Run, Kubernetes, a VPS):
the image listens on `$PORT`; you only need to provide `DATABASE_URL` and `REDIS_URL`.
The schema is created automatically on first start.

```bash
docker build -t url-shortener .
docker run -p 8000:8000 -e DATABASE_URL=... -e REDIS_URL=... url-shortener
```

## Design notes and trade-offs

- **Code length.** The `base62` strategy encodes a 64-bit Snowflake ID, so codes are about
  10 characters long. For the book's 7-character target, use `SHORTENER_STRATEGY=hash`: codes
  are exactly 7 characters, at the cost of an occasional extra DB attempt on collisions.
- **Collision check.** The book says "check whether it exists in the DB, then insert". Here the
  insert happens directly and is retried when the unique constraint on `short_code` is violated.
  This avoids race conditions when several replicas write at the same time.
- **Worker ID.** `MACHINE_ID=auto` increments a counter in Redis and takes a number in 0–1023;
  it stays unique as long as fewer than 1024 instances run at once. For a fixed topology you can
  assign `DATACENTER_ID`/`MACHINE_ID` by hand.
- **Click counter.** Each redirect triggers a single background `UPDATE`. At very high traffic,
  clicks should be buffered in Redis and written in batches (or sent to a queue).
- **Migrations.** The schema is created with `create_all`; add Alembic once the schema needs to evolve.
