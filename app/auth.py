"""API key authentication and rate limiting.

Two separate protections, both required once the gateway is reachable from
anywhere. The key check answers "may you call this at all"; the rate limit
answers "how much", and it is the one that matters if a key ever leaks. A valid
key with no limit is an unbounded bill, which for a project with a $10 budget is
the whole risk.

The limiter counts in Redis rather than in process memory. Per-replica counters
would multiply the effective limit by the replica count, so a "60/min" limit
across three gateways would really be 180/min -- and would change every time
KEDA scaled.
"""

from __future__ import annotations

import hashlib
import logging
import time

from fastapi import HTTPException, Request, status

log = logging.getLogger("auth")

API_KEY_HEADER = "x-api-key"


class AuthConfig:
    def __init__(
        self,
        keys: set[str],
        *,
        rate_limit_per_minute: int,
        enabled: bool,
    ) -> None:
        self.keys = keys
        self.rate_limit_per_minute = rate_limit_per_minute
        self.enabled = enabled


def build_auth_config(raw_keys: str, rate_limit_per_minute: int) -> AuthConfig:
    keys = {k.strip() for k in raw_keys.split(",") if k.strip()}
    enabled = bool(keys)
    if not enabled:
        # Local development convenience. Loud, because shipping this open is
        # exactly the mistake that puts someone else's traffic on your bill.
        log.warning(
            "auth.disabled",
            extra={"reason": "GATEWAY_API_KEYS is empty; every request is allowed"},
        )
    return AuthConfig(keys, rate_limit_per_minute=rate_limit_per_minute, enabled=enabled)


def _key_id(api_key: str) -> str:
    """Short stable identifier for logs and counters -- never the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


async def authorize(request: Request) -> str:
    """FastAPI dependency: verify the key, then charge it against the limit."""
    config: AuthConfig = request.app.state.auth
    if not config.enabled:
        return "anonymous"

    provided = request.headers.get(API_KEY_HEADER, "")
    if provided not in config.keys:
        log.warning(
            "auth.rejected",
            extra={"reason": "missing or unknown key", "path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"A valid {API_KEY_HEADER} header is required.",
        )

    identity = _key_id(provided)
    await enforce_rate_limit(request, identity, config.rate_limit_per_minute)
    return identity


async def enforce_rate_limit(request: Request, identity: str, limit: int) -> None:
    if limit <= 0:
        return
    redis = request.app.state.redis
    window = int(time.time() // 60)
    key = f"ratelimit:{identity}:{window}"
    try:
        count = await redis.incr(key)
        if count == 1:
            # Two minutes, so a request landing at the very end of a window
            # cannot leave an immortal counter if the expire is ever missed.
            await redis.expire(key, 120)
    except Exception as exc:  # noqa: BLE001
        # Fail open: Redis being down should degrade rate limiting, not take
        # the whole gateway offline. Readiness already reports Redis health.
        log.error("ratelimit.unavailable", extra={"error": str(exc)})
        return

    if count > limit:
        log.warning("ratelimit.exceeded", extra={"identity": identity, "count": count})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit of {limit} requests/minute exceeded.",
            headers={"retry-after": str(60 - int(time.time() % 60))},
        )
