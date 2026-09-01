# AI-Router

A cost-aware LLM request router. It classifies each incoming query and dispatches it to the cheapest model tier that can handle it, with response caching, a hand-built load balancer, capability guardrails, and autoscaling.

Everything — including autoscaling — runs on a laptop. Kubernetes is not cloud: `kind` runs a real cluster in Docker containers locally, for free. See [`plan.md`](plan.md) for the full design.

**Status: Phase 2 complete.** The pipeline runs end to end in mock mode with no credentials, and against real models (Ollama locally, Haiku and Sonnet via the Anthropic API) when configured.

## Quick start

```bash
docker compose up --build -d
curl -s localhost:8000/readyz
```

Then send the same prompt twice:

```bash
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"prompt":"Explain why quicksort is O(n log n) on average, step by step."}'
```

The first call takes ~800 ms; the second returns in well under a millisecond. That gap is the cache doing its job — a repeat prompt costs a hash lookup, not a classifier call plus a model call.

```
local   miss   124.11ms  simple lookup keyword
local   HIT      0.76ms  cache hit (originally served by local)
sonnet  miss   803.89ms  multiple reasoning keywords
sonnet  HIT      0.54ms  cache hit (originally served by sonnet)
```

`make demo` runs exactly this (needs `jq`).

## Endpoints

The balancer is the entry point on port 8000; the gateway publishes no host port. `docker compose up -d` starts three gateway replicas, the balancer, and Redis.

## Load balancing

```bash
curl -s localhost:8000/stats | jq '{strategy, dispatched, retried, replicas: [.replicas[] | {url, requests, circuit: .circuit.state}]}'
```

`LB_STRATEGY` is `least_connections` (default) or `round_robin`. Replicas are discovered by DNS, so scaling needs no config change. Each replica has a circuit breaker: three consecutive failures trip it out of rotation for 30s, then one half-open probe decides whether it comes back.

Chaos test — kill a replica mid-load and watch nothing fail:

```bash
python load/driver.py --duration 20 --concurrency 12 &
sleep 7 && docker stop ai-router-gateway-2
```

Measured: 2341 requests, **0 failed**, 6 transparently retried onto surviving replicas.

Each run uses a fresh prompt set, so repeated runs stay comparable rather than replaying the previous run's cache. `--reuse-prompts` measures the warm-cache path deliberately — roughly 2x the throughput and a p50 near 20ms, which is the cache working, not the router.

## Capability guardrails

Each tier declares what it may be asked to do (`app/manifests/*.yaml`). Requests state the capabilities they need and are refused with 403 before any model call:

```bash
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"prompt":"who is Ada Lovelace?","capabilities":["file_access"]}'
```

Deny beats allow, and an unlisted capability is denied — an unlisted capability is an unreviewed one. Cache hits are authorized too, against the tier recorded on the entry; without that, asking once with an allowed capability would let anyone retrieve the answer later with a denied one.

| Endpoint | Purpose |
|---|---|
| `POST /query` | Route and answer a prompt |
| `GET /healthz` | Liveness — is the process alive? Checks nothing else. |
| `GET /readyz` | Readiness — can this replica serve? Pings Redis. Consumed by both the custom load balancer's health check and Kubernetes' readiness probe. |
| `GET /stats` | Per-tier counts, tokens, cost, cache hit rate, savings vs. all-Sonnet |
| `GET /scale-metric` | In-flight concurrency, for KEDA's `metrics-api` scaler in Phase 4 |

## Mock mode

`MOCK_TIERS=true` (the default) serves canned responses with realistic per-tier latency. No credentials, no network, no spend. **Always use it for load tests** — that is what it is for.

Cache keys are namespaced by mode:

```
cache:{schema}:{mock|live}:{sha256(normalized prompt)}
```

Without that `mock|live` segment, a mock-mode load test would write canned responses into the keyspace live requests read from, and the system would start serving `"[mock:sonnet] ..."` as if it were a real answer. Cached entries also record which tier produced them, because the key is prompt-only and the cache is checked *before* classification — so the first tier to answer a prompt answers it forever. That is a deliberate tradeoff; recording the tier makes it measurable rather than invisible.

## Live tiers

Mock mode is the default. To use real models:

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY
MOCK_TIERS=false docker compose up -d
```

Tiers are built independently, so **the local tier works with no Anthropic account at all** — Ollama needs no credentials. Without a key you get a working `local` tier and a 503 with an actionable message if a prompt routes to a paid tier.

Ollama is expected on the host by default (`host.docker.internal:11434`). On macOS that is deliberate: Docker Desktop gives containers no Metal access, so a containerized Ollama runs on CPU and is far slower than the host install — and the local tier is meant to be the fast one. For Linux or CI, a containerized Ollama is available behind a profile:

```bash
docker compose --profile with-ollama up -d
OLLAMA_URL=http://ollama:11434
```

Two knobs worth knowing. `MAX_TOKENS` (default 1024) is a cap, not a target — you are billed for what is generated. `SONNET_THINKING` defaults to `adaptive`, which is how Sonnet 5 runs when unconfigured; setting it to `disabled` trades some quality for lower cost and latency.

## Development

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
make test    # pytest, mock mode, no secrets
make lint    # ruff
```

Requirements are split by purpose. `requirements.txt` is the gateway runtime and is the only one that goes into the image — the ML stack would add several hundred megabytes to an image that never imports it. `requirements-ml.txt` adds scikit-learn/XGBoost/pandas for training (Phase 3); `requirements-dev.txt` adds the test tooling.

## Roadmap

| Phase | Adds | Substrate |
|---|---|---|
| **1** ✅ | Gateway, tier interface, mock mode, cache, stats, logging, live tiers | Compose |
| **2** ✅ | Load balancer, circuit breaker, retry, capability guardrails | Compose |
| 3 | ML classifier, tests, CI | Compose + Actions |
| 4 | Kubernetes manifests, KEDA autoscaling | `kind` (local) |
| Appendix | The same system on a cloud cluster | optional |
