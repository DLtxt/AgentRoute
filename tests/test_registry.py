"""Tier registry construction.

The important case is graceful degradation: no Anthropic key must still give a
working local tier, because Ollama needs no credentials and refusing to start
would make the free tier depend on a paid account.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings, get_settings
from app.tiers.base import MockTier, TierName, TierNotAvailable
from app.tiers.registry import build_registry


def settings(**overrides) -> Settings:
    """Build Settings from the real defaults, then override.

    Deriving from get_settings() rather than a hand-written dict means adding a
    field to Settings does not break every test in this file -- which is exactly
    what happened when auth settings were introduced.
    """
    get_settings.cache_clear()
    base = get_settings()
    get_settings.cache_clear()
    # Neutralise anything a developer's local .env could inject. Since config
    # started loading .env, a real key on disk silently changed what these
    # tests exercised -- passing in CI and failing locally, or worse the
    # reverse. Tests that want credentials set them explicitly.
    base = replace(base, anthropic_api_key=None, gateway_api_keys="")
    return replace(base, **overrides)


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
