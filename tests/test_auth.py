"""API key authentication and Redis-backed rate limiting."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import API_KEY_HEADER, authorize, build_auth_config, enforce_rate_limit


class FakeRedis:
    def __init__(self, *, broken: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}
        self.broken = broken

    async def incr(self, key: str) -> int:
        if self.broken:
            raise ConnectionError("redis is down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl


class FakeApp:
    def __init__(self, auth, redis) -> None:
        self.state = type("S", (), {"auth": auth, "redis": redis})()


class FakeRequest:
    def __init__(self, app, headers: dict | None = None) -> None:
        self.app = app
        self.headers = headers or {}
        self.url = type("U", (), {"path": "/query"})()


def make(keys: str = "secret-a,secret-b", limit: int = 5, broken: bool = False):
    config = build_auth_config(keys, limit)
    redis = FakeRedis(broken=broken)
    return FakeApp(config, redis), redis


def test_empty_key_list_disables_auth():
    config = build_auth_config("", 60)
    assert not config.enabled


def test_keys_are_parsed_and_whitespace_stripped():
    config = build_auth_config(" a , b ,", 60)
    assert config.keys == {"a", "b"}


@pytest.mark.asyncio
async def test_valid_key_is_accepted():
    app, _ = make()
    identity = await authorize(FakeRequest(app, {API_KEY_HEADER: "secret-a"}))
    assert identity != "anonymous"


@pytest.mark.asyncio
async def test_identity_is_not_the_key_itself():
    """Logs and counters must never carry the raw credential."""
    app, redis = make()
    identity = await authorize(FakeRequest(app, {API_KEY_HEADER: "secret-a"}))
    assert "secret-a" not in identity
    assert not any("secret-a" in k for k in redis.counts)


@pytest.mark.asyncio
async def test_missing_key_is_rejected():
    app, _ = make()
    with pytest.raises(HTTPException) as exc:
        await authorize(FakeRequest(app))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_key_is_rejected():
    app, _ = make()
    with pytest.raises(HTTPException) as exc:
        await authorize(FakeRequest(app, {API_KEY_HEADER: "nope"}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_disabled_auth_lets_anonymous_through():
    app, _ = make(keys="")
    assert await authorize(FakeRequest(app)) == "anonymous"


@pytest.mark.asyncio
async def test_rate_limit_trips_after_the_configured_count():
    app, _ = make(limit=3)
    req = FakeRequest(app, {API_KEY_HEADER: "secret-a"})
    for _ in range(3):
        await authorize(req)
    with pytest.raises(HTTPException) as exc:
        await authorize(req)
    assert exc.value.status_code == 429
    assert "retry-after" in exc.value.headers


@pytest.mark.asyncio
async def test_separate_keys_get_separate_budgets():
    app, _ = make(limit=2)
    a = FakeRequest(app, {API_KEY_HEADER: "secret-a"})
    b = FakeRequest(app, {API_KEY_HEADER: "secret-b"})
    await authorize(a)
    await authorize(a)
    await authorize(b)  # b's budget is untouched by a exhausting its own


@pytest.mark.asyncio
async def test_zero_limit_disables_rate_limiting():
    app, redis = make(limit=0)
    req = FakeRequest(app, {API_KEY_HEADER: "secret-a"})
    for _ in range(50):
        await authorize(req)
    assert redis.counts == {}


@pytest.mark.asyncio
async def test_redis_failure_fails_open():
    """Rate limiting should degrade, not take the gateway down with it."""
    app, _ = make(broken=True)
    await enforce_rate_limit(FakeRequest(app), "someone", 1)


@pytest.mark.asyncio
async def test_counter_gets_a_ttl_so_windows_cannot_leak():
    app, redis = make(limit=10)
    await authorize(FakeRequest(app, {API_KEY_HEADER: "secret-a"}))
    assert all(ttl > 60 for ttl in redis.expires.values())
