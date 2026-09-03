"""Replica pool: discovery, health checking, and selection.

Two selection strategies, in the order the plan builds them:

- **round_robin** — a counter mod N. Even distribution, no state per replica,
  no idea which replica is busy.
- **least_connections** — dispatch to whichever healthy replica currently has
  the fewest in-flight requests. Costs one integer per replica and handles
  uneven request durations, which round-robin cannot: a replica stuck on a slow
  Sonnet call keeps receiving its share under round-robin.

Discovery resolves a DNS name to every address behind it, so
`docker compose up --scale gateway=3` works without restating the replica list.
Docker's embedded DNS returns all container IPs for a service name. An explicit
GATEWAY_REPLICAS list overrides discovery when you want fixed targets.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from app.balancer.circuit import CircuitBreaker

log = logging.getLogger("balancer.pool")


class Strategy(StrEnum):
    ROUND_ROBIN = "round_robin"
    LEAST_CONNECTIONS = "least_connections"


@dataclass
class Replica:
    url: str
    healthy: bool = False
    in_flight: int = 0
    requests: int = 0
    failures: int = 0
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def available(self) -> bool:
        return self.healthy and self.breaker.allows_request()

    def snapshot(self) -> dict:
        return {
            "url": self.url,
            "healthy": self.healthy,
            "in_flight": self.in_flight,
            "requests": self.requests,
            "failures": self.failures,
            "circuit": self.breaker.snapshot(),
        }


class NoReplicasAvailable(RuntimeError):
    """Every replica is unhealthy or has its circuit open."""


class ReplicaPool:
    def __init__(
        self,
        *,
        strategy: Strategy,
        failure_threshold: int,
        cooldown_seconds: float,
        static_replicas: list[str] | None = None,
        discovery_host: str | None = None,
        discovery_port: int = 8000,
    ) -> None:
        self._strategy = strategy
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._static = static_replicas or []
        self._host = discovery_host
        self._port = discovery_port
        self._cursor = 0
        self.replicas: dict[str, Replica] = {url: self._new_replica(url) for url in self._static}

    def _new_replica(self, url: str) -> Replica:
        return Replica(
            url=url,
            breaker=CircuitBreaker(
                failure_threshold=self._failure_threshold,
                cooldown_seconds=self._cooldown,
            ),
        )

    # --- discovery -------------------------------------------------------
    async def discover(self) -> None:
        """Refresh the replica set from DNS. No-op when a static list is set."""
        if self._static or not self._host:
            return
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                self._host, self._port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            log.warning("discovery.failed", extra={"host": self._host, "error": str(exc)})
            return

        found = {f"http://{info[4][0]}:{self._port}" for info in infos}
        for url in found - self.replicas.keys():
            self.replicas[url] = self._new_replica(url)
            log.info("replica.added", extra={"url": url})
        for url in self.replicas.keys() - found:
            del self.replicas[url]
            log.info("replica.removed", extra={"url": url})

    # --- health ----------------------------------------------------------
    async def health_check(self, client: httpx.AsyncClient) -> None:
        """Poll /readyz on each replica. The same endpoint Kubernetes probes."""

        async def probe(replica: Replica) -> None:
            try:
                r = await client.get(f"{replica.url}/readyz", timeout=2.0)
                healthy = r.status_code == 200
            except httpx.HTTPError:
                healthy = False
            if healthy != replica.healthy:
                log.info(
                    "replica.health_changed",
                    extra={"url": replica.url, "healthy": healthy},
                )
            replica.healthy = healthy

        await asyncio.gather(*(probe(r) for r in list(self.replicas.values())))

    # --- selection -------------------------------------------------------
    def pick(self, exclude: set[str] | None = None) -> Replica:
        exclude = exclude or set()
        candidates = [r for r in self.replicas.values() if r.available and r.url not in exclude]
        if not candidates:
            raise NoReplicasAvailable(
                f"No replica available out of {len(self.replicas)} known "
                f"({len(exclude)} already tried)"
            )

        if self._strategy is Strategy.LEAST_CONNECTIONS:
            return min(candidates, key=lambda r: (r.in_flight, r.requests))

        # Round-robin over the *available* set, so unhealthy replicas are
        # skipped rather than handed a turn they cannot serve.
        self._cursor = (self._cursor + 1) % len(candidates)
        return candidates[self._cursor]

    def snapshot(self) -> dict:
        return {
            "strategy": self._strategy.value,
            "discovery": "static" if self._static else f"dns:{self._host}:{self._port}",
            "replicas": [r.snapshot() for r in self.replicas.values()],
        }
