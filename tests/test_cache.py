"""Cache key behaviour.

The mode-namespacing test is the important one: without it, a load test run
with MOCK_TIERS=true poisons the keyspace that live requests read from.
"""

from __future__ import annotations

import json

import pytest

from app.cache import Cache, normalize, prompt_digest
from app.tiers.base import TierName, TierResponse


class FakeRedis:
    """Minimal async stand-in. Keeps the cache tests free of a live server."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value


def make_cache(mode: str) -> tuple[Cache, FakeRedis]:
    redis = FakeRedis()
    return Cache(redis, mode=mode, ttl_seconds=60, schema_version="v1"), redis


def response(tier: TierName = TierName.LOCAL) -> TierResponse:
    return TierResponse(
        text="hello", tier=tier, input_tokens=3, output_tokens=2,
        latency_ms=1.0, cost_usd=0.0, mocked=True,
    )


def test_normalize_collapses_whitespace_and_case():
    assert normalize("  Hello   WORLD \n") == "hello world"


def test_digest_is_stable_across_equivalent_prompts():
    assert prompt_digest("Hello  world") == prompt_digest("  hello world ")


def test_key_includes_schema_and_mode():
    cache, _ = make_cache("mock")
    key = cache.key("hi")
    assert key.startswith("cache:v1:mock:")


def test_mock_and_live_modes_do_not_share_keys():
    """A mock-mode load test must not be readable by a live request."""
    mock_cache, _ = make_cache("mock")
    live_cache, _ = make_cache("live")
    assert mock_cache.key("same prompt") != live_cache.key("same prompt")


@pytest.mark.asyncio
async def test_mock_write_is_invisible_to_live_reader():
    redis = FakeRedis()
    mock_cache = Cache(redis, mode="mock", ttl_seconds=60)
    live_cache = Cache(redis, mode="live", ttl_seconds=60)

    await mock_cache.set("what is 2+2", response())
    assert await mock_cache.get("what is 2+2") is not None
    assert await live_cache.get("what is 2+2") is None


@pytest.mark.asyncio
async def test_roundtrip_records_producing_tier():
    cache, _ = make_cache("mock")
    await cache.set("explain recursion", response(TierName.SONNET))
    cached = await cache.get("explain recursion")
    assert cached is not None
    assert cached.tier is TierName.SONNET
    assert cached.text == "hello"


@pytest.mark.asyncio
async def test_miss_returns_none():
    cache, _ = make_cache("mock")
    assert await cache.get("never stored") is None


@pytest.mark.asyncio
async def test_stored_payload_has_expected_shape():
    cache, redis = make_cache("mock")
    await cache.set("hi", response(TierName.HAIKU))
    payload = json.loads(next(iter(redis.store.values())))
    assert set(payload) == {"text", "tier", "created_at"}
    assert payload["tier"] == "haiku"
