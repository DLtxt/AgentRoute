"""Standalone load balancer service.

Fronts N gateway replicas. Round-robin or least-connections selection, health
checks against each replica's /readyz, and a per-replica circuit breaker.

This exists to be understood rather than to be novel. In Phase 4 a Kubernetes
Service does the balancing — and having built this is what makes it possible to
say exactly what that Service does, and exactly what it does not: a Service has
no circuit breaker, which is the gap a service mesh fills.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app.balancer.pool import NoReplicasAvailable, ReplicaPool, Strategy
from app.logging_config import configure_logging

log = logging.getLogger("balancer")


# Hop-by-hop headers are connection-scoped and must not be forwarded to the
# upstream; host and content-length are recomputed by httpx for the new request.
# Everything else is passed through -- notably x-api-key, without which
# authentication cannot work through the proxy at all.
_STRIPPED_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def forward_headers(incoming, client_host: str | None = None) -> dict[str, str]:
    headers = {k: v for k, v in incoming.items() if k.lower() not in _STRIPPED_HEADERS}
    headers.setdefault("content-type", "application/json")
    if client_host:
        headers["x-forwarded-for"] = client_host
    return headers


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _build_pool() -> ReplicaPool:
    static = [u.strip() for u in os.getenv("GATEWAY_REPLICAS", "").split(",") if u.strip()]
    return ReplicaPool(
        strategy=Strategy(os.getenv("LB_STRATEGY", Strategy.LEAST_CONNECTIONS.value)),
        failure_threshold=_int("LB_FAILURE_THRESHOLD", 3),
        cooldown_seconds=float(os.getenv("LB_COOLDOWN_SECONDS", "30")),
        static_replicas=static,
        discovery_host=os.getenv("GATEWAY_HOST", "gateway"),
        discovery_port=_int("GATEWAY_PORT", 8000),
    )


async def _health_loop(app: FastAPI) -> None:
    interval = float(os.getenv("LB_HEALTH_INTERVAL_SECONDS", "2"))
    while True:
        try:
            await app.state.pool.discover()
            await app.state.pool.health_check(app.state.client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            log.warning("health_loop.error", extra={"error": str(exc)})
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    app.state.pool = _build_pool()
    # How long to wait on a replica before treating it as failed. Generous by
    # default because a Sonnet call with adaptive thinking is genuinely slow,
    # but tunable: a hung replica should not hold a connection open forever,
    # and lowering this is what makes the circuit breaker observable live.
    upstream_timeout = float(os.getenv("LB_UPSTREAM_TIMEOUT_SECONDS", "120"))
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(upstream_timeout, connect=5.0))
    app.state.started_at = time.time()
    app.state.dispatched = 0
    app.state.rejected = 0
    app.state.retried = 0

    # Resolve and probe once before serving, so the first request does not race
    # an empty pool.
    await app.state.pool.discover()
    await app.state.pool.health_check(app.state.client)
    task = asyncio.create_task(_health_loop(app))

    log.info("balancer.startup", extra=app.state.pool.snapshot())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await app.state.client.aclose()
        log.info("balancer.shutdown")


app = FastAPI(title="AI Router — Load Balancer", version="0.2.0", lifespan=lifespan)


@app.exception_handler(NoReplicasAvailable)
async def no_replicas(request: Request, exc: NoReplicasAvailable) -> JSONResponse:
    log.error("dispatch.no_replicas", extra={"error": str(exc)})
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Ready only if at least one replica can actually take traffic."""
    pool: ReplicaPool = request.app.state.pool
    usable = [r for r in pool.replicas.values() if r.available]
    if not usable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready", "available_replicas": 0}
    return {"status": "ready", "available_replicas": len(usable)}


@app.get("/stats")
async def stats(request: Request) -> dict:
    """Per-replica distribution — the Phase 2 demo reads this."""
    pool: ReplicaPool = request.app.state.pool
    return {
        "dispatched": request.app.state.dispatched,
        "rejected": request.app.state.rejected,
        "retried": request.app.state.retried,
        **pool.snapshot(),
    }


@app.post("/query")
async def proxy_query(request: Request) -> Response:
    """Dispatch to a replica, retrying elsewhere if one fails.

    Retry is what makes "stop a replica mid-load-test with zero failed
    requests" achievable. Health checks run on an interval, so there is always
    a window in which a replica has died but has not yet been marked down; the
    requests already in flight to it must land somewhere. Without retry those
    surface to the client as 502s -- which is exactly what happened before this
    was added, and what a Kubernetes Service alone would also do.

    The tradeoff: POST is not idempotent in general, so a request that was
    fully processed before its response was lost gets dispatched twice. Here
    that is cheap and safe -- the second attempt almost always hits the cache
    the first attempt populated -- but it would not be safe for an endpoint
    with side effects, and it is a deliberate choice rather than a free win.

    Only connection errors and 5xx are retried. A 4xx is the client's problem
    and every replica would answer it identically.
    """
    pool: ReplicaPool = request.app.state.pool
    client: httpx.AsyncClient = request.app.state.client
    body = await request.body()
    headers = forward_headers(request.headers, request.client.host if request.client else None)

    max_attempts = max(1, min(len(pool.replicas), _int("LB_MAX_ATTEMPTS", 3)))
    tried: set[str] = set()
    last_error = "no attempt was made"

    for attempt in range(max_attempts):
        replica = pool.pick(exclude=tried)
        tried.add(replica.url)
        replica.in_flight += 1
        replica.requests += 1
        request.app.state.dispatched += 1
        started = time.perf_counter()
        try:
            upstream = await client.post(f"{replica.url}/query", content=body, headers=headers)
        except httpx.HTTPError as exc:
            replica.failures += 1
            replica.breaker.record_failure()
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "dispatch.failed",
                extra={
                    "url": replica.url,
                    "attempt": attempt + 1,
                    "error": last_error,
                    "circuit": replica.breaker.state.value,
                },
            )
            continue
        finally:
            replica.in_flight -= 1

        if upstream.status_code >= 500:
            replica.failures += 1
            replica.breaker.record_failure()
            last_error = f"upstream returned {upstream.status_code}"
            log.warning(
                "dispatch.upstream_5xx",
                extra={
                    "url": replica.url,
                    "attempt": attempt + 1,
                    "status": upstream.status_code,
                    "circuit": replica.breaker.state.value,
                },
            )
            continue

        replica.breaker.record_success()
        if attempt:
            request.app.state.retried += 1
        log.info(
            "dispatch.ok",
            extra={
                "url": replica.url,
                "attempt": attempt + 1,
                "status": upstream.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    request.app.state.rejected += 1
    log.error("dispatch.exhausted", extra={"attempts": max_attempts, "error": last_error})
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": f"All {max_attempts} replica attempts failed. Last: {last_error}"},
    )
