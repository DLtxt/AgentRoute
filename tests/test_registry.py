"""Tier registry construction.

The important case is graceful degradation: no Anthropic key must still give a
working local tier, because Ollama needs no credentials and refusing to start
would make the free tier depend on a paid account.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.tiers.base import MockTier, TierName, TierNotAvailable
from app.tiers.registry import build_registry


def settings(**overrides) -> Settings:
    base = dict(
        redis_url="redis://localhost:6379/0",
        ollama_url="http://localhost:11434",
        mock_tiers=True,
        cache_ttl_seconds=60,
        cache_schema_version="v1",
        classifier="rules",
        log_level="INFO",
        anthropic_api_key=None,
        ollama_model="llama3.2:3b",
        ollama_timeout_seconds=120.0,
        max_tokens=1024,
        sonnet_thinking="adaptive",
    )
    return Settings(**{**base, **overrides})


def test_mock_mode_builds_every_tier_without_credentials():
    registry = build_registry(settings(mock_tiers=True))
    assert registry.mocked
    for name in TierName:
        assert isinstance(registry.get(name), MockTier)


def test_live_mode_without_key_still_builds_the_local_tier():
    registry = build_registry(settings(mock_tiers=False))
    assert not registry.mocked
    assert registry.get(TierName.LOCAL).name is TierName.LOCAL


def test_live_mode_without_key_refuses_paid_tiers_with_an_actionable_message():
    registry = build_registry(settings(mock_tiers=False))
    for name in (TierName.HAIKU, TierName.SONNET):
        with pytest.raises(TierNotAvailable, match="ANTHROPIC_API_KEY"):
            registry.get(name)


def test_live_mode_with_key_builds_all_three():
    registry = build_registry(settings(mock_tiers=False, anthropic_api_key="sk-test-not-real"))
    assert {registry.get(n).name for n in TierName} == set(TierName)


def test_mode_namespace_follows_mock_flag():
    assert settings(mock_tiers=True).mode == "mock"
    assert settings(mock_tiers=False).mode == "live"
