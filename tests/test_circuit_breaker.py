"""Circuit breaker state machine.

Time is controlled by monkeypatching the clock rather than sleeping, so the
30-second cooldown is tested in microseconds.
"""

from __future__ import annotations

import pytest

from app.balancer.circuit import CircuitBreaker, CircuitState


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(CircuitBreaker, "_now", lambda self: now["t"])
    return now


def test_starts_closed_and_allows_traffic():
    cb = CircuitBreaker()
    assert cb.state is CircuitState.CLOSED
    assert cb.allows_request()


def test_trips_only_after_the_threshold(clock):
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED, "must not trip early"
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.trips == 1


def test_success_resets_the_failure_run(clock):
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED, "failures must be consecutive to trip"


def test_open_circuit_refuses_traffic_during_cooldown(clock):
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    cb.record_failure()
    assert not cb.allows_request()
    clock["t"] += 29
    assert not cb.allows_request()


def test_half_open_after_cooldown_admits_exactly_one_probe(clock):
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    cb.record_failure()
    clock["t"] += 30
    assert cb.allows_request(), "first request after cooldown is the probe"
    assert cb.state is CircuitState.HALF_OPEN
    assert not cb.allows_request(), "a second concurrent probe must be refused"


def test_successful_probe_closes_the_circuit(clock):
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    cb.record_failure()
    clock["t"] += 30
    cb.allows_request()
    cb.record_success()
    assert cb.state is CircuitState.CLOSED
    assert cb.allows_request()


def test_failed_probe_reopens_and_restarts_the_cooldown(clock):
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    for _ in range(3):
        cb.record_failure()
    clock["t"] += 30
    cb.allows_request()
    cb.record_failure()  # the probe fails
    assert cb.state is CircuitState.OPEN
    assert cb.trips == 2
    clock["t"] += 29
    assert not cb.allows_request(), "cooldown restarts from the failed probe"
    clock["t"] += 1
    assert cb.allows_request()
