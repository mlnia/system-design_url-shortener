import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import unique_url

SHORTEN = "/api/v1/data/shorten"


def test_shorten_and_redirect(client):
    long_url = unique_url()
    res = client.post(SHORTEN, json={"longUrl": long_url})
    assert res.status_code == 201
    body = res.json()
    assert body["longUrl"] == long_url
    assert body["shortUrl"] == f"http://testserver/{body['shortCode']}"

    redirect = client.get(f"/{body['shortCode']}", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == long_url


def test_existing_url_returns_200_with_same_code(client):
    long_url = unique_url()
    first = client.post(SHORTEN, json={"longUrl": long_url})
    second = client.post(SHORTEN, json={"longUrl": long_url})
    assert second.status_code == 200
    assert second.json()["shortCode"] == first.json()["shortCode"]


def test_snake_case_payload_is_accepted(client):
    res = client.post(SHORTEN, json={"long_url": unique_url()})
    assert res.status_code == 201


def test_permanent_redirect_mode(client_factory):
    client = client_factory(redirect_status_code=301)
    code = client.post(SHORTEN, json={"longUrl": unique_url()}).json()["shortCode"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 301


def test_custom_alias_and_conflict(client):
    alias = f"my-{uuid.uuid4().hex[:8]}"
    res = client.post(SHORTEN, json={"longUrl": unique_url(), "customAlias": alias})
    assert res.status_code == 201
    assert res.json()["shortCode"] == alias

    conflict = client.post(SHORTEN, json={"longUrl": unique_url(), "customAlias": alias})
    assert conflict.status_code == 409


def test_reserved_and_invalid_aliases(client):
    assert client.post(SHORTEN, json={"longUrl": unique_url(), "customAlias": "healthz"}).status_code == 400
    assert client.post(SHORTEN, json={"longUrl": unique_url(), "customAlias": "a b"}).status_code == 422
    assert client.post(SHORTEN, json={"longUrl": unique_url(), "customAlias": "ab"}).status_code == 422


def test_invalid_urls_are_rejected(client):
    for bad in ["not a url", "ftp://example.com/file", "javascript:alert(1)", ""]:
        assert client.post(SHORTEN, json={"longUrl": bad}).status_code == 422, bad


def test_too_long_url_is_rejected(client_factory):
    client = client_factory(max_url_length=60)
    res = client.post(SHORTEN, json={"longUrl": "https://example.com/" + "a" * 100})
    assert res.status_code == 422


def test_cannot_shorten_own_links(client):
    assert client.post(SHORTEN, json={"longUrl": "http://testserver/abc"}).status_code == 400


def test_expires_at_must_be_in_future(client):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert client.post(SHORTEN, json={"longUrl": unique_url(), "expiresAt": past}).status_code == 422

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    res = client.post(SHORTEN, json={"longUrl": unique_url(), "expiresAt": future})
    assert res.status_code == 201
    assert res.json()["expiresAt"] is not None


def test_unknown_code_returns_404(client):
    assert client.get("/doesNotExist1", follow_redirects=False).status_code == 404
    assert client.get("/bad.code", follow_redirects=False).status_code == 404
    assert client.get("/api/v1/urls/doesNotExist1").status_code == 404


def test_stats_count_clicks(client):
    code = client.post(SHORTEN, json={"longUrl": unique_url()}).json()["shortCode"]
    for _ in range(3):
        client.get(f"/{code}", follow_redirects=False)
    stats = client.get(f"/api/v1/urls/{code}").json()
    assert stats["clicks"] == 3
    assert stats["shortCode"] == code


def test_rate_limit(client_factory):
    client = client_factory(rate_limit_requests=3, rate_limit_window_seconds=60)
    for _ in range(3):
        res = client.post(SHORTEN, json={"longUrl": unique_url()})
        assert res.status_code == 201
        assert "x-ratelimit-remaining" in res.headers
    blocked = client.post(SHORTEN, json={"longUrl": unique_url()})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    # Redirects are not rate limited.
    assert client.get("/doesNotExist1").status_code == 404


def test_base_url_setting_is_used(client_factory):
    client = client_factory(base_url="https://sho.rt/")
    body = client.post(SHORTEN, json={"longUrl": unique_url()}).json()
    assert body["shortUrl"] == f"https://sho.rt/{body['shortCode']}"


def test_healthz_and_index(client):
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["database"] is True
    index = client.get("/")
    assert index.status_code == 200
    assert "URL Shortener" in index.text
