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
import time
import uuid

import httpx


async def worker(
    client: httpx.AsyncClient,
    url: str,
    counter: itertools.count,
    deadline: float,
    results: dict,
    run_tag: str,
) -> None:
    while time.monotonic() < deadline:
        n = next(counter)
        started = time.perf_counter()
        try:
            r = await client.post(
                url,
                json={"prompt": f"What is fact {run_tag} number {n}?"},
                timeout=30.0,
            )
            elapsed = (time.perf_counter() - started) * 1000
            if r.status_code == 200:
                results["ok"] += 1
                results["latencies"].append(elapsed)
            else:
                results["failed"] += 1
                results["statuses"][r.status_code] = (
                    results["statuses"].get(r.status_code, 0) + 1
                )
        except httpx.HTTPError as exc:
            results["failed"] += 1
            results["errors"][type(exc).__name__] = (
                results["errors"].get(type(exc).__name__, 0) + 1
            )


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
        "--reuse-prompts",
        action="store_true",
        help="Replay a fixed prompt set so the run measures the warm cache instead.",
    )
    args = parser.parse_args()

    run_tag = "fixed" if args.reuse_prompts else uuid.uuid4().hex[:8]

    results = {"ok": 0, "failed": 0, "latencies": [], "statuses": {}, "errors": {}}
    counter = itertools.count()
    deadline = time.monotonic() + args.duration
    started = time.monotonic()

    limits = httpx.Limits(max_connections=args.concurrency * 2)
    async with httpx.AsyncClient(limits=limits) as client:
        await asyncio.gather(
            *(
                worker(client, f"{args.host}/query", counter, deadline, results, run_tag)
                for _ in range(args.concurrency)
            )
        )

    elapsed = time.monotonic() - started
    total = results["ok"] + results["failed"]
    lat = results["latencies"]
    mode = "warm cache (--reuse-prompts)" if args.reuse_prompts else f"cold, run {run_tag}"
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


if __name__ == "__main__":
    asyncio.run(main())
