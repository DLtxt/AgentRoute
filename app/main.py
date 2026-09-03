"""FastAPI gateway.

Per-request order: cache check -> (miss) classify -> guardrail -> dispatch ->
cache write -> return. The cache check precedes classification deliberately: a
repeat prompt then costs a hash lookup rather than a classifier call plus a
model call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.auth import authorize, build_auth_config
from app.cache import Cache
from app.classifier.select import build_classifier
from app.config import get_settings
from app.guardrails import CapabilityDenied, Guardrails, load_manifests
from app.logging_config import configure_logging
from app.models import QueryRequest, QueryResponse
from app.stats import Stats
from app.tiers.base import TierNotAvailable, TierUpstreamError, estimate_cost, estimate_tokens
from app.tiers.registry import TierRegistry, build_registry

log = logging.getLogger("gateway")


# Each pod publishes its own in-flight gauge under this prefix. The key
# carries a short TTL so a pod that dies stops counting within seconds instead
# of inflating the total forever -- the leak that ruled out the Redis
# list-length approach in the first place.
# One hash, one field per pod -- not one key per pod. Reading per-pod keys
# meant SCAN MATCH, which walks the whole keyspace and filters client-side. That
# is O(total keys), and this Redis also holds the response cache: under load the
# cache grew past fifty thousand entries, the metric scrape slowed until KEDA's
# poll timed out, and the autoscaler lost its signal precisely when load was
# highest. HGETALL over one hash is O(pods).
INFLIGHT_KEY = "inflight"
INFLIGHT_TTL_SECONDS = 6
INFLIGHT_SAMPLE_SECONDS = 0.5
# Publish a rolling mean rather than the instantaneous count. In-flight is a
# gauge that swings hard between samples -- one measured run saw it read 35,
# then 4, then 1.75 within thirty seconds under steady load -- and an autoscaler
# fed that signal adds and removes pods chasing noise. Averaging over ten
# seconds tracks sustained pressure and ignores momentary spikes.
INFLIGHT_WINDOW_SECONDS = 10.0


async def _publish_inflight(app: FastAPI) -> None:
    """Publish this pod's smoothed in-flight load to Redis once a second.

    Autoscaling needs the total across every gateway pod. An in-process counter
    can only report one pod's share, and any HTTP endpoint serving it is
    reached through a Service that picks a backend at random -- so a scraper
    would sample one pod and call it the cluster. Redis is the one place every
    pod can write and any component can read the whole picture.
    """
    identity = os.getenv("HOSTNAME") or socket.gethostname()
    window = max(1, int(INFLIGHT_WINDOW_SECONDS / INFLIGHT_SAMPLE_SECONDS))
    samples: deque[int] = deque(maxlen=window)
    ticks = 0
    while True:
        try:
            samples.append(app.state.stats.in_flight)
            ticks += 1
            # Sample twice as often as we publish, so the value written is not
            # decided by whichever instant the publish happened to land on.
            if ticks % 2 == 0:
                mean = sum(samples) / len(samples)
                # Hash fields carry no TTL, so the write timestamp travels with
                # the value and readers drop stale pods themselves. A pod that
                # dies stops counting within INFLIGHT_TTL_SECONDS rather than
                # inflating the total forever.
                await app.state.redis.hset(INFLIGHT_KEY, identity, f"{mean:.3f}:{time.time():.0f}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a metrics blip must not kill serving
            log.warning("inflight.publish_failed", extra={"error": str(exc)})
        await asyncio.sleep(INFLIGHT_SAMPLE_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.settings = settings
    app.state.redis = redis
    app.state.cache = Cache(
        redis,
        mode=settings.mode,
        ttl_seconds=settings.cache_ttl_seconds,
        schema_version=settings.cache_schema_version,
    )
    app.state.stats = Stats(started_at=time.time())
    app.state.tiers = build_registry(settings)
    app.state.guardrails = Guardrails(load_manifests())
    app.state.classifier = build_classifier(settings.classifier)
    app.state.auth = build_auth_config(settings.gateway_api_keys, settings.rate_limit_per_minute)

    publisher = asyncio.create_task(_publish_inflight(app))

    log.info(
        "gateway.startup",
        extra={
            "mode": settings.mode,
            "classifier": settings.classifier,
            "auth_enabled": bool(settings.gateway_api_keys),
            "cache_ttl_seconds": settings.cache_ttl_seconds,
            "redis_url": settings.redis_url,
        },
    )
    try:
        yield
    finally:
        publisher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publisher
        await app.state.tiers.aclose()
        await redis.aclose()
        log.info("gateway.shutdown")


app = FastAPI(title="AI Router", version="0.1.0", lifespan=lifespan)


@app.exception_handler(TierNotAvailable)
async def tier_not_available(request: Request, exc: TierNotAvailable) -> JSONResponse:
    """A configured-but-unimplemented tier is a config problem, not a crash."""
    log.warning("tier.unavailable", extra={"error": str(exc)})
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


@app.exception_handler(CapabilityDenied)
async def capability_denied(request: Request, exc: CapabilityDenied) -> JSONResponse:
    """A guardrail refusal is an authorization result, not an error."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "detail": str(exc),
            "tier": exc.tier.value,
            "capability": exc.capability,
        },
    )


@app.exception_handler(TierUpstreamError)
async def tier_upstream_failed(request: Request, exc: TierUpstreamError) -> JSONResponse:
    """An upstream model provider failed. That is a bad gateway, not a bug."""
    log.error("tier.upstream_failed", extra={"error": str(exc)})
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": str(exc)},
    )


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness: is the process alive? Deliberately checks nothing else."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Readiness: can this replica actually serve traffic?

    The custom load balancer's health check and Kubernetes' readiness probe
    both consume this endpoint. That is the same concept, not a coincidence.
    """
    try:
        await request.app.state.redis.ping()
    except Exception as exc:  # noqa: BLE001 - report any backend failure as not-ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning("readyz.failed", extra={"error": str(exc)})
        return {"status": "not ready", "redis": "unreachable"}
    return {"status": "ready", "redis": "ok"}


@app.get("/stats")
async def stats(request: Request) -> dict:
    return {
        **request.app.state.stats.snapshot(),
        "classifier": request.app.state.classifier.name,
        "guardrails": request.app.state.guardrails.snapshot(),
    }


@app.get("/scale-metric")
async def scale_metric(request: Request) -> dict:
    """In-flight concurrency, for KEDA's metrics-api scaler in Phase 4.

    The obvious choice — KEDA's Redis list-length scaler — does not work here:
    the gateway is synchronous, so nothing is ever enqueued and LLEN would sit
    at zero forever.
    """
    return {"in_flight": request.app.state.stats.in_flight}


@app.post("/query", response_model=QueryResponse)
async def query(
    payload: QueryRequest,
    request: Request,
    identity: str = Depends(authorize),
) -> QueryResponse:
    cache: Cache = request.app.state.cache
    st: Stats = request.app.state.stats

    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    st.inflight_inc()

    try:
        cached = await cache.get(payload.prompt)
        if cached is not None:
            # Authorize the cache hit too. Checking the cache before
            # classifying means a hit has no classifier-assigned tier to test
            # against -- so it is tested against the tier that *produced* the
            # entry, which is why that tier is stored. Without this, asking
            # once with an allowed capability would let anyone retrieve the
            # answer afterwards with a denied one: authorization becomes
            # skippable by being second.
            request.app.state.guardrails.enforce(
                cached.tier, payload.capabilities, request_id=request_id
            )
            input_tokens = estimate_tokens(payload.prompt)
            output_tokens = estimate_tokens(cached.text)
            avoided = estimate_cost(cached.tier, input_tokens, output_tokens)
            st.record_hit(cached.tier, avoided)
            latency_ms = (time.perf_counter() - started) * 1000
            log.info(
                "query.cache_hit",
                extra={
                    "request_id": request_id,
                    "tier": cached.tier.value,
                    "latency_ms": round(latency_ms, 2),
                    "cost_avoided_usd": round(avoided, 6),
                },
            )
            return QueryResponse(
                request_id=request_id,
                response=cached.text,
                tier=cached.tier,
                cached=True,
                reason=f"cache hit (originally served by {cached.tier.value})",
                latency_ms=round(latency_ms, 2),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=0.0,
            )

        st.record_miss()
        decision = request.app.state.classifier.classify(payload.prompt)
        # Guardrail before dispatch: a refused request must cost no model call.
        request.app.state.guardrails.enforce(
            decision.tier, payload.capabilities, request_id=request_id
        )
        registry: TierRegistry = request.app.state.tiers
        result = await registry.get(decision.tier).complete(payload.prompt)
        await cache.set(payload.prompt, result)
        st.record_dispatch(result)

        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "query.dispatched",
            extra={
                "request_id": request_id,
                "identity": identity,
                "tier": result.tier.value,
                "reason": decision.reason,
                "latency_ms": round(latency_ms, 2),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": round(result.cost_usd, 6),
                "mocked": result.mocked,
                "features": decision.features.as_dict(),
            },
        )
        return QueryResponse(
            request_id=request_id,
            response=result.text,
            tier=result.tier,
            cached=False,
            reason=decision.reason,
            latency_ms=round(latency_ms, 2),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=round(result.cost_usd, 6),
        )
    finally:
        st.inflight_dec()
