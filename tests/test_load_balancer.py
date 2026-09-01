"""Replica selection: distribution, and skipping replicas that cannot serve."""

from __future__ import annotations

import pytest

from app.balancer.pool import NoReplicasAvailable, ReplicaPool, Strategy


def pool(strategy: Strategy, n: int = 3) -> ReplicaPool:
    p = ReplicaPool(
        strategy=strategy,
        failure_threshold=3,
        cooldown_seconds=30,
        static_replicas=[f"http://r{i}:8000" for i in range(n)],
    )
    for replica in p.replicas.values():
        replica.healthy = True
    return p


def test_round_robin_distributes_evenly():
    p = pool(Strategy.ROUND_ROBIN)
    counts: dict[str, int] = {}
    for _ in range(300):
        url = p.pick().url
        counts[url] = counts.get(url, 0) + 1
    assert len(counts) == 3, "every replica should receive traffic"
    assert max(counts.values()) - min(counts.values()) <= 1, "distribution should be even"


def test_least_connections_prefers_the_idle_replica():
    p = pool(Strategy.LEAST_CONNECTIONS)
    replicas = list(p.replicas.values())
    replicas[0].in_flight = 5
    replicas[1].in_flight = 0
    replicas[2].in_flight = 3
    assert p.pick().url == replicas[1].url


def test_least_connections_reacts_to_load_round_robin_would_ignore():
    """A replica stuck on a slow call keeps its turn under round-robin."""
    p = pool(Strategy.LEAST_CONNECTIONS)
    replicas = list(p.replicas.values())
    replicas[0].in_flight = 10
    picked = {p.pick().url for _ in range(20)}
    assert replicas[0].url not in picked


def test_unhealthy_replicas_are_skipped():
    p = pool(Strategy.ROUND_ROBIN)
    replicas = list(p.replicas.values())
    replicas[0].healthy = False
    picked = {p.pick().url for _ in range(30)}
    assert replicas[0].url not in picked
    assert len(picked) == 2


def test_open_circuits_are_skipped():
    p = pool(Strategy.LEAST_CONNECTIONS)
    replicas = list(p.replicas.values())
    for _ in range(3):
        replicas[1].breaker.record_failure()
    picked = {p.pick().url for _ in range(30)}
    assert replicas[1].url not in picked


def test_exclude_supports_retry_on_a_different_replica():
    p = pool(Strategy.LEAST_CONNECTIONS)
    first = p.pick()
    second = p.pick(exclude={first.url})
    assert second.url != first.url


def test_raises_when_everything_is_down():
    p = pool(Strategy.ROUND_ROBIN)
    for replica in p.replicas.values():
        replica.healthy = False
    with pytest.raises(NoReplicasAvailable):
        p.pick()


def test_retry_exhausts_cleanly_when_all_replicas_tried():
    p = pool(Strategy.LEAST_CONNECTIONS)
    tried = {r.url for r in p.replicas.values()}
    with pytest.raises(NoReplicasAvailable, match="already tried"):
        p.pick(exclude=tried)
