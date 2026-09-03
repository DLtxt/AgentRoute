"""Concurrent load driver.

A dependency-free stand-in for k6/hey: httpx is already required, so the load
test runs on a fresh clone with no extra install.

Prompts are unique per request *and per run*. The run tag matters more than it
looks: without it every run replays the previous run's prompts, so the second
run onwards measures cache hits rather than routing, reporting throughput that
is inflated and latency that is not the system under test. Pass
--reuse-prompts to deliberately measure the warm-cache path instead.

    python load/driver.py --duration 20 --concurrency 12

While it runs, stop a replica (`docker stop ai-router-gateway-2`) and watch the
failure count stay at zero.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import random
import time
import uuid

import httpx

# Prompts chosen so the rule classifier sends each to a different tier, whose
# mock latencies are 120 / 300 / 800 ms. A uniform workload cannot distinguish
# the two balancing strategies -- round-robin is optimal when every request
# costs the same. The spread is what makes least-connections worth having.
MIXED_SHAPES = (
    ("local", "What is fact {tag} number {n}?"),
    ("haiku", "Implement a helper for case {tag}-{n}."),
    ("sonnet", "```py\n# case {tag}-{n}\nx = {n}\n```"),
)
# Weighted so the slow tier is a minority, as in real traffic: a few expensive
# requests among many cheap ones is exactly the case that starves round-robin.
MIXED_WEIGHTS = (0.6, 0.25, 0.15)


def build_prompt(tag: str, n: int, mixed: bool) -> tuple[str, str]:
    if not mixed:
        return "local", f"What is fact {tag} number {n}?"
    tier, template = random.choices(MIXED_SHAPES, weights=MIXED_WEIGHTS, k=1)[0]
    return tier, template.format(tag=tag, n=n)


async def worker(
    client: httpx.AsyncClient,
    url: str,
    counter: itertools.count,
    deadline: float,
    results: dict,
    run_tag: str,
    mixed: bool,
) -> None:
    while time.monotonic() < deadline:
        n = next(counter)
        tier, prompt = build_prompt(run_tag, n, mixed)
        started = time.perf_counter()
        try:
            r = await client.post(url, json={"prompt": prompt}, timeout=30.0)
            elapsed = (time.perf_counter() - started) * 1000
            if r.status_code == 200:
                results["ok"] += 1
                results["latencies"].append(elapsed)
                results["by_tier"][tier] = results["by_tier"].get(tier, 0) + 1
            else:
                results["failed"] += 1
                results["statuses"][r.status_code] = results["statuses"].get(r.status_code, 0) + 1
        except httpx.HTTPError as exc:
            results["failed"] += 1
            results["errors"][type(exc).__name__] = results["errors"].get(type(exc).__name__, 0) + 1


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * p / 100), len(ordered) - 1)
    return ordered[idx]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument(
        "--mix",
        action="store_true",
        help="Send a mixed workload across all three tiers (120/300/800ms) so "
        "request durations vary. Required to tell the balancing strategies apart.",
    )
    parser.add_argument(
        "--reuse-prompts",
        action="store_true",
        help="Replay a fixed prompt set so the run measures the warm cache instead.",
    )
    args = parser.parse_args()

    run_tag = "fixed" if args.reuse_prompts else uuid.uuid4().hex[:8]

    results = {
        "ok": 0,
        "failed": 0,
        "latencies": [],
        "statuses": {},
        "errors": {},
        "by_tier": {},
    }
    counter = itertools.count()
    deadline = time.monotonic() + args.duration
    started = time.monotonic()

    limits = httpx.Limits(max_connections=args.concurrency * 2)
    async with httpx.AsyncClient(limits=limits) as client:
        await asyncio.gather(
            *(
                worker(
                    client,
                    f"{args.host}/query",
                    counter,
                    deadline,
                    results,
                    run_tag,
                    args.mix,
                )
                for _ in range(args.concurrency)
            )
        )

    elapsed = time.monotonic() - started
    total = results["ok"] + results["failed"]
    lat = results["latencies"]
    mode = "warm cache (--reuse-prompts)" if args.reuse_prompts else f"cold, run {run_tag}"
    mode += ", mixed workload" if args.mix else ", uniform workload"
    print(f"mode        {mode}")
    print(f"requests    {total}  ({total / elapsed:.1f}/s over {elapsed:.1f}s)")
    print(f"ok          {results['ok']}")
    print(f"failed      {results['failed']}")
    if results["statuses"]:
        print(f"  statuses  {results['statuses']}")
    if results["errors"]:
        print(f"  errors    {results['errors']}")
    print(
        f"latency     p50 {percentile(lat, 50):.0f}ms  "
        f"p95 {percentile(lat, 95):.0f}ms  p99 {percentile(lat, 99):.0f}ms"
    )
    if args.mix and results["by_tier"]:
        spread = "  ".join(f"{t}={results['by_tier'].get(t, 0)}" for t, _ in MIXED_SHAPES)
        print(f"tier mix    {spread}")


if __name__ == "__main__":
    asyncio.run(main())
