"""Per-replica circuit breaker.

Three states. CLOSED is normal service. Consecutive failures trip the breaker
to OPEN, which takes the replica out of rotation entirely — no traffic, no
waiting on timeouts. After a cooldown the breaker moves to HALF_OPEN and admits
exactly one probe request: success closes it, failure re-opens it and restarts
the cooldown.

The half-open probe is the part worth understanding. Without it, recovery is
either a guess (reopen blindly and hope) or never (stay open forever). One
probe costs one request to find out, and the failure case costs only that one
request rather than the whole flood you would send by reopening at full rate.

A Kubernetes Service has none of this — it load-balances, but it will keep
sending traffic to an endpoint that fails fast. Circuit breaking is what a
service mesh adds on top.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    trips: int = 0
    _probe_in_flight: bool = field(default=False, repr=False)

    def _now(self) -> float:
        return time.monotonic()

    def allows_request(self) -> bool:
        """Whether a request may be dispatched, advancing state if the cooldown expired."""
        if self.state is CircuitState.CLOSED:
            return True

        if self.state is CircuitState.OPEN:
            if self.opened_at is None:
                return False
            if self._now() - self.opened_at >= self.cooldown_seconds:
                self.state = CircuitState.HALF_OPEN
                self._probe_in_flight = False
            else:
                return False

        # HALF_OPEN: admit exactly one probe at a time.
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self._probe_in_flight = False
        self.state = CircuitState.CLOSED
        self.opened_at = None

    def record_failure(self) -> None:
        self._probe_in_flight = False
        self.consecutive_failures += 1

        if self.state is CircuitState.HALF_OPEN:
            # The probe failed: straight back to OPEN, cooldown restarts.
            self._trip()
        elif self.consecutive_failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        if self.state is not CircuitState.OPEN:
            self.trips += 1
        self.state = CircuitState.OPEN
        self.opened_at = self._now()

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "trips": self.trips,
        }
