"""FastAPI gateway.

Per-request order: cache check -> (miss) classify -> guardrail -> dispatch ->
cache write -> return. The cache check precedes classification deliberately: a
repeat prompt then costs a hash lookup rather than a classifier call plus a
model call.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.cache import Cache
from app.classifier import rules
from app.config import get_settings
from app.guardrails import CapabilityDenied, Guardrails, load_manifests
from app.logging_config import configure_logging
from app.models import QueryRequest, QueryResponse
from app.stats import Stats
from app.tiers.base import TierNotAvailable, TierUpstreamError, estimate_cost, estimate_tokens
from app.tiers.registry import TierRegistry, build_registry

log = logging.getLogger("gateway")


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

    log.info(
        "gateway.startup",
        extra={
            "mode": settings.mode,
            "classifier": settings.classifier,
            "cache_ttl_seconds": settings.cache_ttl_seconds,
            "redis_url": settings.redis_url,
        },
    )
    try:
        yield
    finally:
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
async def query(payload: QueryRequest, request: Request) -> QueryResponse:
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
        decision = rules.classify(payload.prompt)
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
