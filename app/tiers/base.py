"""Model tier interface, plus the single point where mock mode is implemented.

Mock mode lives here, at the interface, rather than in each tier. That is what
lets the whole system run with no credentials and makes load tests free.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class TierName(StrEnum):
    LOCAL = "local"
    HAIKU = "haiku"
    SONNET = "sonnet"


# USD per 1M tokens. Sonnet 5's $2/$10 was introductory at launch and is now
# the standard price; the scheduled rise to $3/$15 was cancelled.
PRICING: dict[TierName, tuple[float, float]] = {
    TierName.LOCAL: (0.0, 0.0),
    TierName.HAIKU: (1.00, 5.00),
    TierName.SONNET: (2.00, 10.00),
}

# Plausible per-tier latency for mock mode, so the tier gradient is visible in
# a demo and load tests exercise realistic timing.
MOCK_LATENCY_MS: dict[TierName, int] = {
    TierName.LOCAL: 120,
    TierName.HAIKU: 300,
    TierName.SONNET: 800,
}


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


def estimate_cost(tier: TierName, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICING[tier]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


@dataclass(frozen=True)
class TierResponse:
    text: str
    tier: TierName
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    mocked: bool = False


class Tier(ABC):
    name: TierName

    @abstractmethod
    async def complete(self, prompt: str) -> TierResponse:
        """Generate a completion for `prompt`."""


class MockTier(Tier):
    """Canned responses with per-tier delay. No network, no credentials."""

    def __init__(self, name: TierName) -> None:
        self.name = name

    async def complete(self, prompt: str) -> TierResponse:
        started = time.perf_counter()
        await asyncio.sleep(MOCK_LATENCY_MS[self.name] / 1000)
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        text = f"[mock:{self.name.value}] response to prompt {digest} ({len(prompt)} chars)"
        input_tokens = estimate_tokens(prompt)
        output_tokens = estimate_tokens(text)
        return TierResponse(
            text=text,
            tier=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
            cost_usd=estimate_cost(self.name, input_tokens, output_tokens),
            mocked=True,
        )


class TierNotAvailable(RuntimeError):
    """Raised when a tier is requested but not configured or available."""


class TierUpstreamError(RuntimeError):
    """Raised when a live tier's upstream provider fails."""
