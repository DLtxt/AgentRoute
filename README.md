# AI-Router

A cost-aware LLM request router. It classifies each incoming query and dispatches it to the cheapest model tier that can handle it, with response caching, a hand-built load balancer, capability guardrails, and autoscaling.

Everything — including autoscaling — runs on a laptop. Kubernetes is not cloud: `kind` runs a real cluster in Docker containers locally, for free. See [`plan.md`](plan.md) for the full design.

**Status: Phase 3 complete** (pipeline; see the labeling caveat below). The pipeline runs end to end in mock mode with no credentials, and against real models (Ollama locally, Haiku and Sonnet via the Anthropic API) when configured.

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

`LB_STRATEGY` is `least_connections` (default) or `round_robin`. `LB_UPSTREAM_TIMEOUT_SECONDS` (default 120) bounds how long a replica may hang before it counts as failed — lower it to make the circuit breaker observable in seconds rather than minutes. Replicas are discovered by DNS, so scaling needs no config change. Each replica has a circuit breaker: three consecutive failures trip it out of rotation for 30s, then one half-open probe decides whether it comes back.

Chaos test — kill a replica mid-load and watch nothing fail:

```bash
python load/driver.py --duration 20 --concurrency 12 &
sleep 7 && docker stop ai-router-gateway-2
```

Measured: 2341 requests, **0 failed**, 6 transparently retried onto surviving replicas.

`--mix` sends a workload spread across all three tiers (120/300/800ms mock latencies) instead of a uniform one — needed to tell the balancing strategies apart at all, since round-robin is optimal when every request costs the same.

Each run uses a fresh prompt set, so repeated runs stay comparable rather than replaying the previous run's cache. `--reuse-prompts` measures the warm-cache path deliberately — roughly 2x the throughput and a p50 near 20ms, which is the cache working, not the router.

### Measured limits

Numbers from an 8-core M-series laptop with the load generator on the same machine, mock mode:

| | |
|---|---|
| Peak throughput | ~480 req/s |
| Single load-driver ceiling | ~225 req/s — **run 2-3 drivers in parallel or you measure the client** |
| Throughput vs. gateway replicas | flat: 1 replica 479/s, 3 replicas 483/s, 6 replicas 468/s |
| CPU share at peak | balancer ~53%, each gateway 14-21% |

Two things worth knowing. The balancer carries roughly 3x a gateway's CPU because every request passes through it, and **adding gateway replicas does not raise throughput here** — a hand-built userspace proxy is a very different thing from a Kubernetes Service, which balances in the kernel. That gap is the honest answer to "why does Phase 4 scale the gateway?": autoscaling replicas only helps once the work per request is real, not when a single proxy fronts everything.

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

## Classifier

Three implementations behind one `classify(prompt) -> Decision` interface, switchable at runtime so they can be A/B'd on live traffic:

```bash
CLASSIFIER=rules   docker compose up -d gateway   # hand-written rules, no model needed
CLASSIFIER=logreg  docker compose up -d gateway   # baseline
CLASSIFIER=xgb     docker compose up -d gateway   # gradient boosting
```

Both ML models use the same hand-crafted features — not embeddings — so a feature-importance chart explains exactly why a prompt routed where it did. The logistic regression is not a formality: it is the baseline that makes choosing XGBoost defensible rather than decorative. If it wins, that is the result.

`logreg` and `xgb` need a trained model **and** the ML stack in the image, which is opt-in because it takes the image from 187 MB to 1.27 GB:

```bash
python -m app.classifier.train                 # writes models/
INSTALL_ML=true docker compose build gateway
INSTALL_ML=true CLASSIFIER=xgb docker compose up -d gateway
```

Without either, the gateway falls back to rules and logs a warning — a fresh clone still serves, but the downgrade is never silent.

### ⚠️ Neither model beats guessing on the current dataset

The labeling run completed — 291 outcome-labeled prompts — and the result is negative:

```
majority-class baseline      0.698
ceiling on these features    0.852
rows with identical features
  but conflicting labels     122/291 (42%)

logreg   0.685  -0.013  NO BETTER THAN GUESSING
xgb      0.589  -0.109  NO BETTER THAN GUESSING
```

Always answering "haiku" scores 0.698. Both trained models do worse. This is a dataset problem, not a model-choice problem, and it has two causes worth understanding:

**The corpus is template-generated**, so prompts differ only by a filled-in noun and collide in feature space. `What is the capital of Portugal?` and `What is the capital of Iceland?` produce identical feature vectors — correctly, since they are the same task.

**The judge labels them inconsistently.** Those two got `local` and `haiku`. `Who is Alan Turing?` got `haiku` while `Who is Grace Hopper?` got `sonnet`. 42% of rows sit in a group of identical vectors carrying conflicting labels, which no model can separate.

The ceiling of 0.852 says real signal exists (+0.155 over baseline); the models capture none of it. Fixing this means a genuinely varied corpus rather than template fills, and reducing judge noise — a stricter rubric, or majority vote over several judge samples. Both require re-labeling (~$4).

Reporting this rather than a tuned number is the point. `train.py` now prints the baseline and the ceiling next to every score, because 0.685 reads as a mediocre result until you know that guessing scores 0.698.

The dataset comes from running every prompt through every tier and labeling each with *the cheapest tier that produced an acceptable answer* — a judgment made by outcome, not by intuition. A model trained on intuition labels only learns to reproduce the rule classifier, so comparing the two would measure nothing.

```bash
python -m app.classifier.label --dry-run    # cost estimate: ~$3.90 for 300 prompts
python -m app.classifier.label --limit 20   # cheap real subset first
python -m app.classifier.label              # the full run
python -m app.classifier.train              # retrain on real labels
python -m app.classifier.train --balanced   # weight classes inversely to frequency
```

Needs `ANTHROPIC_API_KEY` and `MOCK_TIERS=false`. Progress checkpoints after every prompt, so an interrupted run resumes rather than re-spending; `--fresh` discards and starts over.

Acceptability is graded by Sonnet rather than by hand, which is what makes 300 prompts × 3 tiers tractable — the alternative is reading 900 generations. It is a real shortcut with a real cost: the judge is Sonnet grading Sonnet's output among others, so spot-check a sample of rows before trusting the labels.

Two things that made this harder than it looks, both worth knowing before changing the code:

- **The judge must have thinking disabled.** Sonnet 5 runs adaptive thinking by default and thinking tokens count against `max_tokens`; grading three real answers consumed the whole budget and returned *no text*, which the parser read as "every tier unacceptable" and skipped the prompt. It now raises instead of silently mislabeling.
- **Answers must not be truncated.** A reply cut off mid-sentence by `max_tokens` reads as wrong to any grader. The run uses its own larger budget and asks every tier for a concise answer, identically, so the comparison stays fair. Thinking stays *on* for the answering tiers, since disabling it would understate the capability being measured. Truncations are counted and reported.

## Gateway auth

```bash
GATEWAY_API_KEYS=your-key RATE_LIMIT_PER_MINUTE=120 docker compose up -d
curl -X POST localhost:8000/query -H 'x-api-key: your-key' ...
```

Empty `GATEWAY_API_KEYS` disables auth entirely — fine locally, never with a public address. The rate limit counts in Redis rather than per process, so the limit is shared across replicas instead of multiplied by them; with three gateways, a per-process counter would have made `120/min` really mean `360/min`, and the number would change every time KEDA scaled.

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
