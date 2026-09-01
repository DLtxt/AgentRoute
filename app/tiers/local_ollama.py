"""Local model tier, served by Ollama over its REST API.

Ollama runs on the host by default rather than in a container. On macOS a
containerized Ollama has no Metal access and falls back to CPU, which is
dramatically slower — the whole point of this tier is that it is the fast,
free one. The containerized service is available behind a Compose profile for
Linux and CI, where that tradeoff does not apply.
"""

from __future__ import annotations

import time

import httpx

from app.tiers.base import Tier, TierName, TierResponse, TierUpstreamError, estimate_cost


class OllamaError(TierUpstreamError):
    """Ollama was unreachable or returned an error."""


class OllamaTier(Tier):
    name = TierName.LOCAL

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 120.0) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
        )

    async def complete(self, prompt: str) -> TierResponse:
        started = time.perf_counter()
        try:
            r = await self._client.post(
                "/api/generate",
                json={"model": self._model, "prompt": prompt, "stream": False},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama unreachable at {self._client.base_url}: {exc}") from exc

        body = r.json()
        # Ollama reports real token counts; no estimation needed.
        input_tokens = int(body.get("prompt_eval_count", 0))
        output_tokens = int(body.get("eval_count", 0))
        return TierResponse(
            text=body.get("response", ""),
            tier=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
            cost_usd=estimate_cost(self.name, input_tokens, output_tokens),
            mocked=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
