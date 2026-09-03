"""Capability guardrails: per-tier authorization before dispatch."""

from __future__ import annotations

import pytest

from app.guardrails import CapabilityDenied, Guardrails, load_manifests
from app.tiers.base import TierName


@pytest.fixture
def guardrails() -> Guardrails:
    return Guardrails(load_manifests())


def test_every_tier_ships_a_manifest():
    manifests = load_manifests()
    assert set(manifests) == set(TierName)


def test_allowed_capability_passes(guardrails):
    guardrails.enforce(TierName.LOCAL, ["text_generation"], request_id="r1")
    assert guardrails.violations == 0


def test_denied_capability_is_refused_and_counted(guardrails):
    with pytest.raises(CapabilityDenied, match="explicitly denied"):
        guardrails.enforce(TierName.LOCAL, ["code_execution"], request_id="r2")
    assert guardrails.violations == 1


def test_unlisted_capability_is_denied_by_default(guardrails):
    """An unlisted capability is an unreviewed one; default-permit would let
    every new capability name grant itself access."""
    with pytest.raises(CapabilityDenied, match="not in the allowed list"):
        guardrails.enforce(TierName.SONNET, ["launch_missiles"], request_id="r3")


def test_capability_allowed_on_one_tier_can_be_denied_on_another(guardrails):
    guardrails.enforce(TierName.SONNET, ["code_execution"], request_id="r4")
    with pytest.raises(CapabilityDenied):
        guardrails.enforce(TierName.HAIKU, ["code_execution"], request_id="r5")


def test_file_access_is_denied_on_every_tier(guardrails):
    for tier in TierName:
        with pytest.raises(CapabilityDenied):
            guardrails.enforce(tier, ["file_access"], request_id="r6")


def test_first_violation_in_a_list_stops_the_check(guardrails):
    with pytest.raises(CapabilityDenied) as exc:
        guardrails.enforce(
            TierName.LOCAL,
            ["text_generation", "web_search", "code_execution"],
            request_id="r7",
        )
    assert exc.value.capability == "web_search"


def test_a_cached_answer_is_still_authorized(guardrails):
    """Regression: a cache hit must not bypass the capability check.

    The cache is consulted before classification, so a hit carries no
    classifier verdict -- it is checked against the tier recorded on the entry.
    Before this was enforced, asking once with an allowed capability let any
    later request retrieve the same answer with a denied one.
    """
    # The tier that produced the entry is what the hit is authorized against.
    guardrails.enforce(TierName.LOCAL, ["text_generation"], request_id="warm")
    with pytest.raises(CapabilityDenied, match="explicitly denied"):
        guardrails.enforce(TierName.LOCAL, ["file_access"], request_id="hit")
