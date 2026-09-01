"""Redis cache-aside.

Key shape: cache:{schema}:{mode}:{sha256(normalized prompt)}

The `mode` segment is not cosmetic. Load tests are meant to run with
MOCK_TIERS=true; without a mode namespace every canned mock response lands in
the same keyspace live requests read from, and the demo starts serving
"[mock:local] ..." out of cache. Prompt-only keys make that a certainty.

The stored value records which tier produced the answer. Because the key is
prompt-only and the cache is checked before classification, the first tier to
answer a prompt answers it forever — recording the tier makes that tradeoff
measurable instead of invisible, and leaves room for a minimum-tier policy
later without a cache wipe.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.tiers.base import TierName, TierResponse

_WHITESPACE = re.compile(r"\s+")


def normalize(prompt: str) -> str:
    return _WHITESPACE.sub(" ", prompt.strip().lower())


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(normalize(prompt).encode()).hexdigest()


@dataclass(frozen=True)
class CachedResponse:
    text: str
    tier: TierName
    created_at: str


class Cache:
    def __init__(
        self,
        redis: Redis,
        *,
        mode: str,
        ttl_seconds: int,
        schema_version: str = "v1",
    ) -> None:
        self._redis = redis
        self._mode = mode
        self._ttl = ttl_seconds
        self._schema = schema_version

    def key(self, prompt: str) -> str:
        return f"cache:{self._schema}:{self._mode}:{prompt_digest(prompt)}"

    async def get(self, prompt: str) -> CachedResponse | None:
        raw = await self._redis.get(self.key(prompt))
        if raw is None:
            return None
        payload = json.loads(raw)
        return CachedResponse(
            text=payload["text"],
            tier=TierName(payload["tier"]),
            created_at=payload["created_at"],
        )

    async def set(self, prompt: str, response: TierResponse) -> None:
        payload = {
            "text": response.text,
            "tier": response.tier.value,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await self._redis.set(self.key(prompt), json.dumps(payload), ex=self._ttl)
