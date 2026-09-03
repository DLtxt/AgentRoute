"""In-process counters backing GET /stats and GET /scale-metric.

Deliberately per-process, not shared in Redis. Once the load balancer fronts
several replicas, per-replica stats are exactly what is wanted — the Phase 2
demo is "watch the request distribution across replicas", which a shared
counter would erase.

`in_flight` is the reason this exists now rather than later: /stats wants it
immediately, and KEDA's metrics-api scaler will read it in Phase 4. Retrofitting
a counter through every request path afterwards is worse than having it early.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.tiers.base import PRICING, TierName, estimate_cost


@dataclass
class TierStats:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms_total: float = 0.0


@dataclass
class Stats:
    started_at: float = 0.0
    in_flight: int = 0
    peak_in_flight: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cost_avoided_by_cache_usd: float = 0.0
    hits_by_origin_tier: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tiers: dict[str, TierStats] = field(
        default_factory=lambda: {t.value: TierStats() for t in TierName}
    )

    # --- in-flight -------------------------------------------------------
    def inflight_inc(self) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def inflight_dec(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    # --- recording -------------------------------------------------------
    def record_dispatch(self, response) -> None:
        s = self.tiers[response.tier.value]
        s.requests += 1
        s.input_tokens += response.input_tokens
        s.output_tokens += response.output_tokens
        s.cost_usd += response.cost_usd
        s.latency_ms_total += response.latency_ms

    def record_miss(self) -> None:
        self.cache_misses += 1

    def record_hit(self, tier: TierName, avoided_usd: float) -> None:
        self.cache_hits += 1
        self.hits_by_origin_tier[tier.value] += 1
        self.cost_avoided_by_cache_usd += avoided_usd

    # --- derived ---------------------------------------------------------
    def snapshot(self) -> dict:
        total_requests = sum(t.requests for t in self.tiers.values())
        total_cost = sum(t.cost_usd for t in self.tiers.values())

        # What the same traffic would have cost routed entirely to Sonnet.
        all_sonnet_cost = sum(
            estimate_cost(TierName.SONNET, t.input_tokens, t.output_tokens)
            for t in self.tiers.values()
        )

        lookups = self.cache_hits + self.cache_misses
        return {
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate": round(self.cache_hits / lookups, 4) if lookups else 0.0,
                "cost_avoided_usd": round(self.cost_avoided_by_cache_usd, 6),
                "hits_by_origin_tier": dict(self.hits_by_origin_tier),
            },
            "tiers": {
                name: {
                    "requests": t.requests,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cost_usd": round(t.cost_usd, 6),
                    "avg_latency_ms": (
                        round(t.latency_ms_total / t.requests, 2) if t.requests else 0.0
                    ),
                    "price_per_1m": {
                        "input": PRICING[TierName(name)][0],
                        "output": PRICING[TierName(name)][1],
                    },
                }
                for name, t in self.tiers.items()
            },
            "cost": {
                "dispatched_requests": total_requests,
                "actual_usd": round(total_cost, 6),
                "all_sonnet_usd": round(all_sonnet_cost, 6),
                "saved_vs_all_sonnet_usd": round(all_sonnet_cost - total_cost, 6),
                "saved_pct": (
                    round(100 * (all_sonnet_cost - total_cost) / all_sonnet_cost, 2)
                    if all_sonnet_cost
                    else 0.0
                ),
            },
        }
