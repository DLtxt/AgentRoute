"""Tier response shape and cost arithmetic."""

from __future__ import annotations

import pytest

from app.tiers.anthropic_tier import MODEL_IDS
from app.tiers.base import PRICING, MockTier, TierName, estimate_cost


def test_model_ids_carry_no_date_suffix():
    assert MODEL_IDS[TierName.HAIKU] == "claude-haiku-4-5"
    assert MODEL_IDS[TierName.SONNET] == "claude-sonnet-5"


def test_local_tier_is_free():
    assert PRICING[TierName.LOCAL] == (0.0, 0.0)
    assert estimate_cost(TierName.LOCAL, 10_000, 10_000) == 0.0


def test_cost_uses_published_per_million_rates():
    # Sonnet 5: $2 in / $10 out per 1M tokens.
    assert estimate_cost(TierName.SONNET, 1_000_000, 0) == pytest.approx(2.0)
    assert estimate_cost(TierName.SONNET, 0, 1_000_000) == pytest.approx(10.0)
    # Haiku 4.5: $1 in / $5 out.
    assert estimate_cost(TierName.HAIKU, 1_000_000, 0) == pytest.approx(1.0)
    assert estimate_cost(TierName.HAIKU, 0, 1_000_000) == pytest.approx(5.0)


def test_sonnet_costs_more_than_haiku_for_identical_usage():
    assert estimate_cost(TierName.SONNET, 1000, 1000) > estimate_cost(TierName.HAIKU, 1000, 1000)


@pytest.mark.asyncio
async def test_mock_tier_reports_itself_as_mocked():
    response = await MockTier(TierName.HAIKU).complete("hello")
    assert response.mocked is True
    assert response.tier is TierName.HAIKU
    assert response.input_tokens > 0 and response.output_tokens > 0
