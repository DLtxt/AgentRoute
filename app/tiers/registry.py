"""Tier construction and lifetime.

Tiers are built once at startup, not per request. The live tiers hold HTTP
connection pools; constructing them per request would open a new pool for every
query and throw away connection reuse.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.tiers.base import MockTier, Tier, TierName, TierNotAvailable

log = logging.getLogger("tiers")


class TierRegistry:
    def __init__(self, tiers: dict[TierName, Tier], *, mocked: bool) -> None:
        self._tiers = tiers
        self.mocked = mocked

    def get(self, name: TierName) -> Tier:
        try:
            return self._tiers[name]
        except KeyError as exc:
            raise TierNotAvailable(
                f"Tier {name.value!r} is not available: ANTHROPIC_API_KEY is not set. "
                "Set it in .env, or set MOCK_TIERS=true to run without credentials."
            ) from exc

    async def aclose(self) -> None:
        for tier in self._tiers.values():
            closer = getattr(tier, "aclose", None)
            if closer is not None:
                await closer()


def build_registry(settings: Settings) -> TierRegistry:
    if settings.mock_tiers:
        log.info("tiers.built", extra={"mode": "mock", "tiers": [t.value for t in TierName]})
        return TierRegistry({name: MockTier(name) for name in TierName}, mocked=True)

    # Imported lazily so mock mode never needs the SDK or an API key.
    from app.tiers.local_ollama import OllamaTier

    # Build every tier whose credentials are present, rather than refusing to
    # start when any are missing. Ollama needs none, so the free local tier
    # works without an Anthropic account; a request routed to a tier that was
    # not built fails at dispatch with an actionable message instead.
    tiers: dict[TierName, Tier] = {
        TierName.LOCAL: OllamaTier(
            settings.ollama_url,
            settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
        )
    }

    if settings.anthropic_api_key:
        from anthropic import AsyncAnthropic

        from app.tiers.anthropic_tier import AnthropicTier

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        tiers[TierName.HAIKU] = AnthropicTier(
            TierName.HAIKU, client, max_tokens=settings.max_tokens
        )
        tiers[TierName.SONNET] = AnthropicTier(
            TierName.SONNET,
            client,
            max_tokens=settings.max_tokens,
            thinking=settings.sonnet_thinking,
        )
    else:
        log.warning(
            "tiers.paid_unavailable",
            extra={"reason": "ANTHROPIC_API_KEY is not set; only the local tier is available"},
        )

    log.info(
        "tiers.built",
        extra={
            "mode": "live",
            "available": sorted(t.value for t in tiers),
            "ollama_url": settings.ollama_url,
            "ollama_model": settings.ollama_model,
            "max_tokens": settings.max_tokens,
            "sonnet_thinking": settings.sonnet_thinking,
        },
    )
    return TierRegistry(tiers, mocked=False)
