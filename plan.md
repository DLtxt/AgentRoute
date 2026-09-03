# AI Router — Implementation Reference

What is built, how it works, and what is not built yet. Operational instructions are in [`README.md`](README.md).

---

## Contents
- [Status](#status)
- [Architecture](#architecture)
- [Gateway](#gateway)
- [Classifier](#classifier)
- [Model tiers](#model-tiers)
- [Cache](#cache)
- [Load balancer](#load-balancer)
- [Capability guardrails](#capability-guardrails)
- [Authentication and rate limiting](#authentication-and-rate-limiting)
- [Dataset and training](#dataset-and-training)
- [Observability](#observability)
- [Deployment topology](#deployment-topology)
- [Kubernetes](#kubernetes)
- [Autoscaling](#autoscaling)
- [Configuration contract](#configuration-contract)
- [Continuous integration](#continuous-integration)
- [Tests](#tests)
- [Not built](#not-built)

---

## Status

| Phase | Contents | State |
|---|---|---|
| 1 | Gateway, tier interface, mock mode, cache, stats, structured logging, live tiers | Complete |
| 2 | Load balancer, circuit breaker, retry, capability guardrails | Complete |
| 3 | ML classifier pipeline, auth, rate limiting, CI | Complete |
| 4 | Kubernetes manifests, KEDA autoscaling | Complete |
| Appendix | Cloud deployment | Not started |

Everything runs locally: Docker Compose for the inner loop, `kind` for the Kubernetes deployment. `kind` runs a real cluster in Docker containers on the machine, so no cloud account is involved at any point.

---

## Architecture

```
Client
  |
  v
Load balancer          least-connections or round-robin,
  |                    health checks, circuit breaker, retry
  v
Gateway replicas (3)
  |
  +-- authenticate (x-api-key) and charge the rate limit
  |
  v
Redis cache check ----------- HIT --> authorize, then respond
  |
  MISS
  |
  v
Classifier             rules | logreg | xgb
  |
  v
Capability guardrail   per-tier YAML manifest
  |
  v
Dispatch to one tier   Ollama | Haiku | Sonnet
  |
  v
Cache write --> respond
```

Two ordering decisions worth noting. The cache is checked before classification, so a repeat prompt costs a hash lookup rather than a classifier call plus a model call; a hit is authorized against the tier recorded on the cache entry, since it has no classifier verdict of its own. The balancer fronts the gateway rather than the model tiers, because the gateway is the only component actually hosted here.

---

## Gateway

`app/main.py`. FastAPI, stateless, three replicas by default.

Per-request order: authenticate → rate limit → cache check → classify → guardrail → dispatch → cache write → respond. An in-flight counter is incremented on entry and decremented in a `finally`, exposed at `/stats` and `/scale-metric`.

Endpoints: `POST /query`, `GET /healthz` (liveness, checks nothing else), `GET /readyz` (pings Redis), `GET /stats`, `GET /scale-metric`.

---

## Classifier

Three implementations behind one `classify(prompt) -> Decision` interface, selected by `CLASSIFIER` at startup. `Decision` carries the tier, a human-readable reason, and the extracted features.

**Rules** (`app/classifier/rules.py`) — routes on prompt length, code-fence presence, sentence count, question marks, and three keyword lists. Always returns a tier; there is no reject path.

**Logistic regression and XGBoost** (`app/classifier/ml.py`) — trained on the same eight hand-crafted features, not embeddings, so feature importance explains each routing decision. Models are pickled with the feature-name list they were trained on, and loading verifies it matches; a mismatch is refused rather than silently mis-mapping inputs.

If a model is missing or unreadable, the gateway falls back to rules and logs a warning.

**Features** (`app/classifier/features.py`): `char_length`, `approx_tokens`, `has_code_fence`, `sentence_count`, `question_marks`, `complex_keyword_hits`, `code_keyword_hits`, `simple_keyword_hits`.

---

## Model tiers

`app/tiers/`. Common interface: `async def complete(prompt) -> TierResponse`, carrying text, token counts, latency, estimated cost, and whether the response was truncated or mocked.

| Tier | Implementation | Price per 1M tokens |
|---|---|---|
| `local` | Ollama REST, `llama3.2:3b` | free |
| `haiku` | Anthropic SDK, `claude-haiku-4-5` | $1 in / $5 out |
| `sonnet` | Anthropic SDK, `claude-sonnet-5` | $2 in / $10 out |

Token counts come from each provider's own reporting rather than estimation. Haiku and Sonnet share one implementation, differing only by model id and price.

**Mock mode** is implemented once at the interface, not per tier. `MOCK_TIERS=true` returns canned responses with per-tier latency (120/300/800 ms), so the system runs with no credentials and load tests cost nothing.

Tiers are built once at startup by `app/tiers/registry.py` and closed on shutdown, so HTTP connection pools are reused. They are built independently: the local tier works without an Anthropic key, and a prompt routed to an unbuilt tier returns 503 naming the missing variable.

---

## Cache

`app/cache.py`. Cache-aside in Redis, TTL only — a cached completion has no upstream source of truth that can change, so expiry is sufficient.

Key: `cache:{schema}:{mock|live}:{sha256(normalized prompt)}`, where normalization lowercases and collapses whitespace. The mode segment keeps mock-mode responses out of the keyspace live requests read. The schema segment allows the stored format to change without serving stale shapes.

Value records the text, the tier that produced it, and a timestamp. Because the key is prompt-only and the cache precedes classification, the first tier to answer a prompt answers it thereafter; recording the tier makes that measurable and lets a hit be authorized against it.

---

## Load balancer

`app/load_balancer.py` and `app/balancer/`. A standalone service, the entry point on port 8000.

**Selection** — `least_connections` (fewest in-flight, default) or `round_robin` (counter modulo the available set, so unhealthy replicas are skipped rather than handed a turn).

**Discovery** — resolves a DNS name to every address behind it and reconciles the replica set each health-check cycle. Docker's embedded DNS returns all container IPs for a service name, so scaling needs no configuration change. `GATEWAY_REPLICAS` overrides with a fixed list.

**Health checks** — polls each replica's `/readyz` on an interval, the same endpoint a Kubernetes readiness probe would use.

**Circuit breaker** (`app/balancer/circuit.py`) — three states per replica. Consecutive failures trip CLOSED to OPEN, removing it from rotation. After a cooldown the next request moves it to HALF_OPEN and admits exactly one probe; success closes it, failure reopens it and restarts the cooldown. The transition is evaluated lazily when a request needs a replica, so there is no background timer.

**Retry** — a connection failure or 5xx is retried on a different replica, up to `LB_MAX_ATTEMPTS` or the replica count. A 4xx is not retried. Health checks run on an interval, so a replica can be dead but not yet marked down; retry is what keeps those in-flight requests from surfacing as 502s.

**Header forwarding** — client headers pass through except hop-by-hop ones and those httpx recomputes; `x-forwarded-for` is added.

---

## Capability guardrails

`app/guardrails.py`, manifests in `app/manifests/{local,haiku,sonnet}.yaml`.

Each tier declares `allowed_capabilities` and `denied_capabilities`. A request states the capabilities it needs; the selected tier's manifest is checked before dispatch, so a refusal costs no model call and is logged with the tier, capability, reason, and request id.

Deny beats allow. A capability in neither list is denied — an unlisted capability is an unreviewed one, and defaulting to permitted would let every new capability name grant itself access.

Cache hits are authorized too, against the tier recorded on the entry.

---

## Authentication and rate limiting

`app/auth.py`. A FastAPI dependency on `POST /query`.

**Authentication** — callers send `x-api-key`, checked against `GATEWAY_API_KEYS`. An empty list disables authentication and logs a warning. Keys are never logged; a short hash identifies the caller in logs and counters.

**Rate limiting** — a fixed-window counter in Redis, keyed by the caller hash and the minute. Counting in Redis rather than per process means the limit is shared across replicas instead of multiplied by them. If Redis is unreachable the limiter fails open, so rate limiting degrades rather than taking the gateway down.

---

## Dataset and training

**Corpus** (`app/classifier/corpus.py`) — 130 curated prompts across three difficulty bands, interleaved so any prefix stays balanced. Curated rather than template-generated: template fills produced prompts differing by a single noun, 69% of which shared an identical feature vector with another prompt. The curated set is at 19%.

**Labeling** (`app/classifier/label.py`) — each prompt is answered by every tier and labeled with the cheapest tier that produced an acceptable answer, judged by Sonnet. Around $1.69 for the full corpus.

- The free local tier is sampled three times and passes on a majority vote. Its answer quality on borderline prompts is close to a coin flip, and voting turns that into a stable label at no generation cost.
- Every tier receives the same concision instruction and a token budget large enough to finish, since an answer truncated mid-sentence grades as wrong.
- The judge runs with thinking disabled and returns five labeled verdicts, parsed by marker rather than line position. An empty judge response aborts the run instead of recording labels.
- Progress checkpoints after every prompt. Re-running resumes; `--fresh` starts over; `--synthetic` writes placeholders with no API calls.
- Each label is written alongside its evidence — every tier's answer and the judge's raw verdicts — to a `.audit.jsonl` sidecar, so a label can be checked rather than trusted.

**Inspection** (`app/classifier/inspect.py`) — reports class balance, distinct feature vectors, conflicting rows, and the achievable ceiling, warning when a class is absent or under 10% and when many rows share features but disagree on the label. `--show N` prints the full evidence behind one label; `--conflicts` lists groups of identical features with disagreeing labels; `--samples` and `--label` filter the rows.

**Training** (`app/classifier/train.py`) — logistic regression as the baseline, then XGBoost, on the same features and the same split. Reports both confusion matrices with the held-out sample size, feature importance, and each model's accuracy against the majority-class baseline and the achievable ceiling. `--balanced` applies inverse-frequency class weights. Models and metrics are written to `models/`.

---

## Observability

Structured JSON logging, one object per line on stdout, with a request id threaded through cache, classifier, guardrail, and dispatch. Uvicorn's own output is routed through the same formatter.

`/stats` on the gateway reports per-tier request counts, tokens, cost, average latency, cache hit rate, cost avoided, hits by originating tier, guardrail violations, and in-flight concurrency. `/stats` on the balancer reports per-replica request counts, failures, circuit state, dispatched, rejected, and retried totals.

Gateway stats are per process by design: once several replicas are running, per-replica figures are what the distribution demo needs.

---

## Deployment topology

| Component | Replicas | Notes |
|---|---|---|
| Load balancer | 1 | Entry point, publishes port 8000 |
| Gateway | 3 | Publishes no host port |
| Redis | 1 | No persistence — a cache and a scaling signal, not a datastore |
| Ollama | host, or 1 via profile | Host by default; a container gets no Metal access on macOS |

Images: the gateway image is 187 MB by default and 1.27 GB with `INSTALL_ML=true`, which is why the ML stack is opt-in. Trained models mount as a read-only volume, so retraining needs no rebuild.

---

## Kubernetes

Manifests under `k8s/`, applied with Kustomize. `kind-config.yaml` defines a three-node cluster; the nodes are containers on one Docker host, so this gives real scheduling semantics rather than real capacity.

| Object | Notes |
|---|---|
| `redis` Deployment + Service | One replica, no persistence |
| `gateway` Deployment | Two replicas as a floor; KEDA owns the count |
| `gateway` Service | **Headless** (`clusterIP: None`) so DNS returns every pod IP |
| `gateway-direct` Service | Ordinary ClusterIP over the same pods, for comparison |
| `balancer` Deployment + Service | Two replicas, so the tier in front of an autoscaling backend is not itself a single point of failure |
| `balancer-nodeport` | Local overlay only; kind maps host 8000 to node port 30080 |
| `router-config` ConfigMap | Non-secret configuration |
| `router-secrets` Secret | Only `ANTHROPIC_API_KEY` and `GATEWAY_API_KEYS` |

The gateway Service is headless because the balancer's discovery resolves a name to every address behind it. A normal ClusterIP resolves to one virtual IP, which would collapse the pool to a single entry and hide every scale event.

The Secret carries only the two secret keys. Building it with `--from-env-file=.env` loads every line of `.env`, and because `secretRef` follows `configMapRef` in the pod spec, any overlapping key silently overrides the ConfigMap — a developer's `MOCK_TIERS=false` would put the whole cluster into live mode. `make k8s-secret` copies only the two.

Probes reuse the endpoints the custom balancer already polls: `/healthz` for liveness, `/readyz` for readiness. Every pod carries resource requests and memory limits.

Images are side-loaded with `kind load docker-image`, since kind nodes cannot see the host's Docker daemon.

---

## Autoscaling

KEDA `ScaledObject` on the gateway Deployment, 2 to 8 replicas, driven by a `metrics-api` trigger reading `avg_in_flight` from the balancer's `/scale-metric`.

**Why not the Redis list-length scaler.** The obvious choice, but the gateway is synchronous — a client POSTs `/query` and blocks — so nothing is ever enqueued and `LLEN` would sit at zero forever.

**Why the balancer serves the metric.** The scaler reads one number from one URL. Aimed at the gateway Service it would reach one arbitrary pod and report that pod's counter, which says nothing about cluster-wide load.

**Why the value comes from Redis rather than the balancer's own counters.** There are two balancer replicas, and the Service fronting them hands a scraper one at random, so in-process figures report a random fraction of the load. Every gateway pod publishes its own gauge instead, and any balancer returns the same total.

**Why a hash, not a key per pod.** Reading per-pod keys means `SCAN MATCH`, which walks the entire keyspace and filters client-side. This Redis also holds the response cache: under load it grew past fifty thousand entries, the scrape slowed until KEDA's poll timed out, and the autoscaler lost its signal exactly when load was highest. `HGETALL` over one hash is O(pods) — measured at 4–8 ms against a 60,000-key database.

**Why the value is smoothed.** In-flight is a gauge that swings hard between samples; an unsmoothed signal read 35, then 4, then 1.75 within thirty seconds under steady load, and the autoscaler chased the noise. Each pod publishes a ten-second rolling mean. Hash fields carry no TTL, so a write timestamp travels with the value and readers drop pods that have stopped reporting.

Scale-down uses a sixty-second HPA stabilization window. KEDA's own `cooldownPeriod` only applies when scaling to zero; above zero it delegates to the HPA, whose default five-minute window makes a demo look stuck.

Measured: 2 pods to 5 under roughly 200 req/s from three parallel load drivers, settling back to 2 about thirty seconds after load stopped, with 10 failed requests out of ~29,500 during pod churn.

---

## Configuration contract

Every service address and secret comes from an environment variable; nothing reads a host path or assumes a working directory. `.env` is loaded for host-run tools and read natively by Compose, with real environment variables taking precedence.

Health endpoints are split: `/healthz` answers whether the process is alive, `/readyz` whether it can serve. The balancer's health check and a Kubernetes readiness probe consume the same endpoint.

Images are built multi-arch (`linux/amd64,linux/arm64`) in CI, since development is on Apple Silicon and CI runners are x86.

---

## Continuous integration

`.github/workflows/ci.yml`, three jobs, no repository secrets.

1. **lint-and-test** — ruff check, ruff format check, pytest in mock mode.
2. **smoke** — brings up the full stack, waits for the balancer to find all replicas, asserts a query routes correctly, asserts the second identical query is a cache hit, asserts a denied capability returns 403, then kills a replica mid-load and asserts zero dropped requests.
3. **image** — multi-arch build pushed to GHCR on `main`, using the automatic `GITHUB_TOKEN`. The repository name is lowercased, since Docker tags require it.

---

## Tests

85 tests, all runnable in mock mode with no credentials.

| File | Covers |
|---|---|
| `test_cache.py` | Key shape, normalization, mock/live isolation, roundtrip, TTL payload |
| `test_circuit_breaker.py` | Trip threshold, cooldown, half-open probe, failed-probe reopen |
| `test_load_balancer.py` | Distribution for both strategies, skipping unhealthy and open-circuit replicas, retry exclusion, header forwarding |
| `test_guardrails.py` | Allow, deny, unlisted-denied, per-tier differences, cache-hit authorization |
| `test_auth.py` | Key parsing, acceptance and rejection, identity hashing, limit enforcement, per-key budgets, fail-open |
| `test_registry.py` | Mock and live construction, graceful degradation without a key |
| `test_dataset.py` | Feature/vector alignment, label encoding, roundtrip, corpus properties |
| `test_classifier_select.py` | Selection, fallback, feature-set mismatch, corrupt model |
| `test_tiers.py` | Model ids, pricing arithmetic |
| `test_label.py` | Judge verdict parsing, empty-response handling |
| `test_inspect.py` | Audit loading, conflict detection |
| `test_load_balancer.py` | Also covers the scale metric averaging over healthy replicas |

---

## Not built

**Cloud deployment** — provisioning, cloud overlay, teardown.

**Not planned** — multi-agent orchestration, a hand-built Redis, consistent hashing (there are no sessions), external tracing, a service mesh.
