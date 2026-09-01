"""Haiku and Sonnet tiers, via the official Anthropic SDK.

Both paid tiers share one implementation — they differ only by model id and
price, which is exactly the kind of duplication a common interface exists to
avoid. Token counts come from the API's own usage report, so cost figures on
these tiers are exact rather than estimated.
"""

from __future__ import annotations

import time

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    NotFoundError,
    RateLimitError,
)

from app.tiers.base import Tier, TierName, TierResponse, TierUpstreamError, estimate_cost

# Model ids are complete as written — no date suffixes.
MODEL_IDS: dict[TierName, str] = {
    TierName.HAIKU: "claude-haiku-4-5",
    TierName.SONNET: "claude-sonnet-5",
}


class AnthropicError(TierUpstreamError):
    """The Anthropic API call failed or was refused."""


class AnthropicTier(Tier):
    def __init__(
        self,
        name: TierName,
        client: AsyncAnthropic,
        *,
        max_tokens: int = 1024,
        thinking: str = "adaptive",
    ) -> None:
        self.name = name
        self._client = client
        self._model = MODEL_IDS[name]
        self._max_tokens = max_tokens
        self._thinking = thinking

    def _request_kwargs(self, prompt: str) -> dict:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Sonnet 5 runs adaptive thinking when `thinking` is omitted. That is
        # the model's intended operation, but it costs tokens and latency —
        # which works against this project's cost thesis — so it is switchable.
        if self._thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        return kwargs

    async def complete(self, prompt: str) -> TierResponse:
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(**self._request_kwargs(prompt))
        except NotFoundError as exc:
            raise AnthropicError(f"Model {self._model!r} not found: {exc}") from exc
        except RateLimitError as exc:
            raise AnthropicError(f"Rate limited on {self._model}: {exc}") from exc
        except APIStatusError as exc:
            raise AnthropicError(
                f"Anthropic API error {exc.status_code} on {self._model}: {exc}"
            ) from exc
        except APIConnectionError as exc:
            raise AnthropicError(f"Could not reach the Anthropic API: {exc}") from exc

        # Safety classifiers can decline a request with HTTP 200 and
        # stop_reason "refusal", so check before reading content.
        if getattr(message, "stop_reason", None) == "refusal":
            detail = getattr(message, "stop_details", None)
            raise AnthropicError(f"{self._model} declined the request: {detail}")

        # Responses may carry thinking blocks alongside text; take the text.
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens
        return TierResponse(
            text=text,
            tier=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
            cost_usd=estimate_cost(self.name, input_tokens, output_tokens),
            mocked=False,
        )
