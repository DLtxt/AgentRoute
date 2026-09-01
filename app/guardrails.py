"""Capability guardrails: per-tier authorization, enforced before dispatch.

Each tier declares what it may and may not be asked to do. A request states the
capabilities it needs; if the selected tier is not authorized for one of them,
the request is refused before any model is called — so a violation costs
nothing and is recorded rather than silently downgraded.

Deny wins over allow. A capability absent from both lists is denied: an
unlisted capability is an unreviewed one, and defaulting to "permitted" would
mean every new capability name silently grants itself access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.tiers.base import TierName

log = logging.getLogger("guardrails")

MANIFEST_DIR = Path(__file__).parent / "manifests"


class CapabilityDenied(RuntimeError):
    """The selected tier is not authorized for a requested capability."""

    def __init__(self, tier: TierName, capability: str, reason: str) -> None:
        self.tier = tier
        self.capability = capability
        self.reason = reason
        super().__init__(
            f"Tier {tier.value!r} is not authorized for capability {capability!r}: {reason}"
        )


@dataclass(frozen=True)
class Manifest:
    tier: TierName
    allowed: frozenset[str]
    denied: frozenset[str]

    def check(self, capability: str) -> None:
        if capability in self.denied:
            raise CapabilityDenied(self.tier, capability, "explicitly denied")
        if capability not in self.allowed:
            raise CapabilityDenied(self.tier, capability, "not in the allowed list")


def load_manifests(directory: Path | None = None) -> dict[TierName, Manifest]:
    directory = directory or MANIFEST_DIR
    manifests: dict[TierName, Manifest] = {}
    for tier in TierName:
        path = directory / f"{tier.value}.yaml"
        raw = yaml.safe_load(path.read_text())
        manifests[tier] = Manifest(
            tier=tier,
            allowed=frozenset(raw.get("allowed_capabilities") or ()),
            denied=frozenset(raw.get("denied_capabilities") or ()),
        )
    return manifests


class Guardrails:
    def __init__(self, manifests: dict[TierName, Manifest]) -> None:
        self._manifests = manifests
        self.violations = 0

    def enforce(
        self, tier: TierName, capabilities: list[str], *, request_id: str
    ) -> None:
        manifest = self._manifests[tier]
        for capability in capabilities:
            try:
                manifest.check(capability)
            except CapabilityDenied as exc:
                self.violations += 1
                log.warning(
                    "guardrail.violation",
                    extra={
                        "request_id": request_id,
                        "tier": tier.value,
                        "capability": capability,
                        "reason": exc.reason,
                    },
                )
                raise

    def snapshot(self) -> dict:
        return {
            "violations": self.violations,
            "tiers": {
                tier.value: {
                    "allowed": sorted(m.allowed),
                    "denied": sorted(m.denied),
                }
                for tier, m in self._manifests.items()
            },
        }
