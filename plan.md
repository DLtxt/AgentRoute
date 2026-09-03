# AI Router & Autoscaling Platform — Project Plan

A cost-aware LLM request router that classifies incoming queries and dispatches each to the cheapest model tier that can handle it, with response caching, a hand-built load balancer, capability guardrails, and autoscaling.

**The entire system — including autoscaling — runs on a laptop with no cloud account.** Kubernetes is not cloud: `kind` runs a real Kubernetes cluster in Docker containers locally, for free. Cloud deployment is an optional appendix that adds credibility, not capability.

---

## Contents
- [Scope and sequencing](#scope-and-sequencing)
- [What runs where](#what-runs-where)
- [The MVP](#the-mvp)
- [Goals](#goals)
- [Architecture](#architecture)
- [The portability contract](#the-portability-contract)
- [Tech stack](#tech-stack)
- [Day 0 setup](#day-0-setup)
- [Phase 1 — the pipeline, on Compose](#phase-1--the-pipeline-on-compose)
- [Phase 2 — load balancer and guardrails](#phase-2--load-balancer-and-guardrails)
- [Phase 3 — ML, tests, CI](#phase-3--ml-tests-ci)
- [Phase 4 — local Kubernetes and autoscaling](#phase-4--local-kubernetes-and-autoscaling)
- [Appendix — cloud deployment (optional, deferred)](#appendix--cloud-deployment-optional-deferred)
- [Component deep-dives](#component-deep-dives)
- [Repo structure](#repo-structure)
- [Metrics to log](#metrics-to-log)
- [Budget](#budget)
- [Hardware requirements](#hardware-requirements)
- [Explicitly out of scope](#explicitly-out-of-scope-and-why)
- [Interview talking points](#interview-talking-points)
- [Stretch ideas](#stretch-ideas)

---

## Scope and sequencing

Four phases, all local. Phases are an ordering, not a schedule — there is no deadline attached to any of them, and nothing here should be scoped down to hit a date.

| Phase | Adds | Substrate |
|---|---|---|
| 1 | Gateway, tiers, mock mode, cache, logging | Docker Compose |
| 2 | Load balancer, circuit breaker, guardrails | Docker Compose |
| 3 | ML classifier, tests, CI | Docker Compose + GitHub Actions |
| 4 | Kubernetes manifests, KEDA autoscaling | `kind` (local) |
| Appendix | Same system on a real cloud cluster | GKE / DOKS — **deferred** |

**Why the appendix is deferred and not cancelled.** Everything the project claims to *do* is provable on a laptop after Phase 4. Cloud deployment proves it runs on infrastructure you don't control, which is a real and separate claim — worth making eventually, worth nothing until the rest works.

**Why Phase 4 uses `kind` and not Docker Compose.** Compose cannot autoscale. `docker compose up --scale gateway=3` is a manual knob, not a controller reacting to load. Autoscaling requires an orchestrator, and `kind` is one that runs entirely on your machine for $0. Compose stays in the repo as the fast inner-loop dev environment — both are used, for different things, exactly as they would be on a real team.

---

## What runs where

The common confusion is that load balancing and ML somehow depend on Kubernetes or on the cloud. They don't. Only one row in this table needs an orchestrator, and none needs a cloud account.

| Capability | What it actually requires | Local? |
|---|---|---|
| Routing (rule-based) | nothing | ✅ |
| Response caching | Redis | ✅ |
| Load balancing + circuit breaker | 2+ gateway replicas via Compose `--scale` | ✅ no orchestrator needed |
| ML classifier | scikit-learn / XGBoost + a labeled dataset | ✅ pure Python |
| Capability guardrails | nothing | ✅ |
| **Autoscaling** | **an orchestrator — `kind` + KEDA** | ✅ `kind` is local |
| Cloud deployment | a cloud account | ❌ the only genuinely cloud-bound item |

---

## The MVP

The smallest thing that runs end to end, in two rings.

### Ring 0 — no credentials, nothing to sign up for

- FastAPI gateway: `POST /query`, `GET /healthz`, `GET /readyz`, `GET /stats`
- Tier interface `async def complete(prompt) -> TierResponse`, with `MOCK_TIERS=true` implemented **once at that interface**
- Rule-based classifier — length, code fences, sentence count, keywords
- Redis cache-aside, mode-namespaced key (see [Redis cache](#4-redis-cache))
- In-flight request counter (needed by `/stats` now, by KEDA in Phase 4)
- Structured JSON logging
- `docker-compose.yml`: gateway + redis

Runs with zero secrets. This is the piece everything else hangs off — it either works or it doesn't.

### Ring 1 — the MVP proper

Ring 0 plus:

- Real Ollama tier (`llama3.2:3b` over REST)
- Real Haiku and Sonnet tiers via the Anthropic SDK
- Cost accounting in `/stats`: per-tier counts, tokens, dollars spent, estimated savings vs. routing everything to Sonnet
- `ollama` added to the Compose file

Three containers: gateway, redis, ollama. It routes each query to the cheapest capable tier, caches the answer, and reports what it saved.

**Done when:** two identical curls — the second returns in single-digit milliseconds.

**Deliberately not in the MVP:** the load balancer (meaningless against a single replica — its value is redistribution under replica failure), the ML classifier (rules work; XGBoost needs the dataset first), Kubernetes, KEDA, CI, multi-arch builds. Guardrails are borderline — roughly an hour of work — so include them if you want the capability story intact from the start.

---

## Goals
- A system that runs end-to-end locally with one command, autoscaling included.
- Learn hands-on: load-balancing algorithms, cache-aside design, ML classification with hand-crafted features, Kubernetes primitives and autoscaling, CI/CD, and capability-based authorization.
- Stay under $10 total.

---

## Architecture

### Request path

```
Client
  |
  v
Load balancer  (custom: round-robin -> least-connections,
  |             health checks + circuit breaker per replica)
  v
Gateway replicas (N)
  |
  v
Redis cache check
  |-- HIT --> Response          (no classifier call, no model call)
  |
  MISS
  |
  v
Classifier (rule-based -> XGBoost, A/B switchable)
  |
  |-- routes to exactly one of --
  |
  +--> Local model (Ollama)   free, fastest, least capable
  +--> Claude Haiku            cheap, fast, mid capability
  +--> Claude Sonnet           priciest, most capable
  |
  v
Capability guardrail check (per-tier YAML manifest)
  |
  v
Cache write --> Response
```

Two deliberate ordering choices:

1. **Cache check precedes classification.** A repeat prompt costs a hash lookup — not a classifier call plus a model call. Cheap and worth doing, but *not* free: a cache hit has no classifier-assigned tier, so it has no tier to authorize against. Building Phase 2 proved this the hard way — guardrails were bypassable simply by asking once with an allowed capability and again with a denied one. A hit is now authorized against the tier recorded on the cache entry, which is a second reason that field earns its place.
2. **The load balancer fronts the gateway, not the model tiers.** Haiku and Sonnet are hosted APIs; Anthropic already load-balances those. The only thing you host, and therefore the only thing you can balance, is your own gateway.

### Deployment topology

| Component | Replicas | Scales? | Why |
|---|---|---|---|
| Gateway | 2 → N | **Yes**, via KEDA in Phase 4 | Stateless, ~150 MB per pod, and it's what's actually under concurrent pressure |
| Ollama | 1 | **No** | Each replica loads its own copy of the model weights — scaling this is a RAM disaster and buys nothing |
| Redis | 1 | No | Shared cache; scaling it would fragment the cache |
| Custom load balancer | 1 | No | Compose entry point only — see [Load balancer](#5-load-balancer--the-piece-to-get-right) |

---

## The portability contract

Follow these from the start and the optional cloud appendix is a day's work rather than a week's.

1. **No hardcoded hostnames.** Every service address comes from an env var (`REDIS_URL`, `OLLAMA_URL`). Never `localhost` in application code.
2. **All config via environment.** No config files read from host paths, no absolute paths, no assumptions about the working directory.
3. **Secrets only via env vars.** Never in a ConfigMap, never in an image, never committed. `.env` locally → K8s Secret in the cluster.
4. **Build multi-arch images.** `docker buildx build --platform linux/amd64,linux/arm64`. If you're on Apple Silicon and skip this, your images will not run on x86 nodes, and the error message won't make it obvious why.
5. **One registry everywhere.** Push to GHCR (free for public repos) from Phase 3. Keep `kind load docker-image` for the inner dev loop — pulling from GHCR on every iteration is much slower — and use the registry for anything that needs to match what a cluster would really pull.
6. **Real health endpoints.** `/healthz` (liveness — is the process alive?) and `/readyz` (readiness — can it serve traffic? Redis reachable?). Your custom load balancer's health check and Kubernetes' readiness probe consume the same endpoint. That's not a coincidence; it's the same concept.
7. **Mock mode.** `MOCK_TIERS=true` stubs the paid tiers with canned responses and realistic per-tier delays. Makes CI runnable without secrets, load tests free, and the repo runnable by a reviewer with no API key.
8. **Resource requests and limits on every pod.** Required for meaningful scheduling, and forces you to actually know what your services consume.

**Note on replica discovery.** The balancer discovers replicas by resolving a DNS name to every address behind it, so `docker compose up` with three gateway replicas needs no replica list. An explicit `GATEWAY_REPLICAS` list overrides discovery when fixed targets are wanted. Under Kubernetes the same mechanism needs a headless Service to return pod IPs — see [Load balancer](#5-load-balancer--the-piece-to-get-right).

---

## Tech stack

### Accounts
| What | For | Cost |
|---|---|---|
| Anthropic API key (console.anthropic.com) | Haiku + Sonnet tiers | ~$5–8 total; set a hard spend limit before you start |
| GitHub | Repo, Actions, GHCR image registry | Free (public repo) |
| Google Cloud **or** DigitalOcean | Appendix only — not needed | ~$1–3, ephemeral |

### Local installs
| Tool | Purpose | Phase |
|---|---|---|
| Docker Desktop / Engine + Compose | Container runtime, inner dev loop | 1 |
| Ollama | Local model tier | 1 |
| Python 3.11+ | `fastapi`, `uvicorn`, `httpx`, `anthropic`, `redis`, `pyyaml`, `scikit-learn`, `xgboost`, `pandas`, `python-dotenv`, `pytest`, `ruff`, `slowapi` | 1 |
| kind | Local Kubernetes cluster | 4 |
| kubectl | Cluster CLI | 4 |
| Helm | Installs KEDA | 4 |
| k6 or hey | Load generation | 3, 4 |
| gcloud CLI **or** doctl | Cloud cluster provisioning | Appendix |

### Models
| Tier | Model | How | Cost per 1M tokens |
|---|---|---|---|
| Local | Llama 3.2 3B or Phi-4-mini (Q4) | `ollama pull llama3.2:3b` | $0 |
| Mid | Claude Haiku 4.5 | `claude-haiku-4-5` | $1 in / $5 out |
| High | Claude Sonnet 5 | `claude-sonnet-5` | $2 in / $10 out |

Sonnet 5's $2/$10 rate was announced at launch as introductory pricing through 31 Aug 2026. **It is now the standard price** — the scheduled increase to $3/$15 was cancelled. Do not budget for a price rise.

Check `ollama.com/library` for the current best small-model tag before pulling, and `platform.claude.com/docs/en/about-claude/pricing` before building your cost chart — both move.

---

## Day 0 setup

This repository already exists and is already a git repo with a remote, so **do not run `mkdir` or `git init`** — that would nest a second, unrelated repo inside this one.

```bash
cd /path/to/AI-Router
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt      # or the explicit package list, before that file exists
ollama pull llama3.2:3b              # ~2 GB download
```

Then start Docker Desktop. `cp .env.example .env` comes **after** scaffolding creates `.env.example` — that file does not exist yet, so the copy will fail if you run it early.

Verify Ollama is reachable before writing any router code:
```bash
curl http://localhost:11434/api/generate -d '{"model":"llama3.2:3b","prompt":"hi","stream":false}'
```

### On the virtualenv

`source venv/bin/activate` does not care about your working directory — all it does is prepend `venv/bin` to `$PATH` **for that one shell session**. Two consequences:

- `cd` elsewhere while activated and the packages are still available; activation follows the shell, not the folder.
- Open a fresh terminal, `cd` here *without* activating, and you get `ModuleNotFoundError` despite being in the right directory.

So: every new terminal, `cd` here and `source venv/bin/activate`.

**Do not install these packages globally instead.** Three reasons that bite in practice: different projects need conflicting versions of the same library, and the scientific stack here (numpy/scipy/pandas/scikit-learn/xgboost) pins tightly enough that this happens early; `pip freeze` against a global install can't tell you which packages this project needs, so `requirements.txt` ends up wrong or bloated — and that file is consumed by both CI and the Docker image; and there is no clean uninstall. The gateway ultimately runs *in a container built from `requirements.txt`*, so the venv's job is to mirror what that image will contain. A global install cannot do that job.

For command-line *tools* you run rather than import (`ruff`, `pytest`, `httpie`), `pipx` gives you install-once-use-everywhere safely — each tool in its own isolated venv, command on your `PATH`. Libraries your code imports stay per-project.

`venv/` and `.env` are already covered by `.gitignore`.

---

## Phase 1 — the pipeline, on Compose

**Status: complete.** See the repository for what shipped; the notes below are the original specification.

- FastAPI gateway with `POST /query`, `GET /healthz`, `GET /readyz`, `GET /stats`
- Rule-based classifier: prompt length, code-fence regex, sentence count, keyword flags
- Three tier handlers behind a common interface (Ollama via REST, Haiku and Sonnet via the Anthropic SDK)
- **Mock mode** (`MOCK_TIERS=true`) with per-tier canned latency — build this first; everything downstream depends on it
- Redis cache-aside, checked *before* classification, with a mode-namespaced key
- **In-flight request counter** — increment on entry, decrement in a `finally`. `/stats` wants it now; KEDA will want it in Phase 4. Cheaper to build now than to retrofit.
- Structured JSON logging: request id, cache hit/miss, tier chosen, latency, token counts, cost estimate
- `docker-compose.yml`: gateway, redis, ollama

**Done when:** `docker compose up` then two identical curls — the second returns in single-digit milliseconds. That contrast is your first demo moment.

## Phase 2 — load balancer and guardrails

**Status: complete.**

- Extract the load balancer into its own service (`app/load_balancer.py`, own container), fronting 2+ gateway replicas
- Round-robin first. Then least-connections. Then health checking against `/readyz`. Then a circuit breaker (trip after 3 consecutive failures, 30s cooldown, half-open retry)
- **Retry on a different replica.** Health checks run on an interval, so there is always a window where a replica has died but is not yet marked down; requests already in flight to it must land somewhere. Without retry they surface as 502s — measured at 3 failures out of 1910 before it was added, and 0 out of 2341 after. This is also precisely what a bare Kubernetes Service does *not* do for you.
- The gateway service must **publish no host ports** — only the load balancer does. Otherwise `--scale gateway=3` collides on the port.
- Per-tier YAML capability manifests + a FastAPI dependency enforcing them before dispatch, logging every violation
- Compose now runs: load balancer, gateway ×3, redis, ollama

**Done when:** you can stop one gateway replica mid-load-test and watch traffic redistribute with zero failed requests, and `/stats` shows the per-replica distribution.

## Phase 3 — ML, tests, CI

**Status: pipeline complete; the labeling run is still pending an API key.**

- **Build the labeled dataset by outcome, not intuition.** Collect ~300 prompts, run each through all three tiers, compare outputs, label each with *the cheapest tier that produced an acceptable answer*. This costs about $2.50 in API spend. Budget the *human* time honestly: 300 prompts × 3 tiers is 900 generations to read and adjudicate. That is the real cost of this step, and it is worth paying — it's the difference between a real dataset and a model trained to predict your own guesses. If you want to cut the reading down without cutting the dataset, have Sonnet pre-grade and hand-review only the disagreements.
- Train logistic regression as a baseline, then XGBoost. Same hand-crafted features. Report both confusion matrices — having the baseline is what makes the XGBoost choice defensible instead of decorative. **Print the held-out sample size next to the matrices**; with ~300 samples across 3 classes the test set is small enough that a narrow win may not be a real result, and saying so is the stronger answer.
- Keep classifier selection behind `CLASSIFIER=rules|logreg|xgb` so all three are A/B-able at runtime
- **Tests worth having** (all runnable in mock mode, no secrets):
  - load balancer distributes evenly across replicas over N requests
  - circuit breaker trips after consecutive failures and recovers after cooldown
  - cache returns on hit, populates on miss, and expires on TTL
  - cache does **not** serve a mock response to a live request, or vice versa
  - guardrail blocks a denied capability and logs the violation
- GitHub Actions: `ruff` + `pytest` (mock mode, no API key needed) + `docker buildx` multi-arch build + push to GHCR
- API key header check on the gateway, **plus rate limiting** (`slowapi` or equivalent). The key check alone is not enough: a leaked or brute-forced key with no rate limit is an unbounded bill.

**Done when:** CI is green on a fresh clone with zero repository secrets configured.

## Phase 4 — local Kubernetes and autoscaling

Still entirely local. `kind` runs Kubernetes in Docker containers on your machine; no cloud account is involved.

- `kind` cluster, 3 nodes (`kind-config.yaml`), so scheduling behaves realistically. Note the nodes are containers sharing one host's RAM — you get real scheduling semantics, not real capacity.
- `k8s/base/`: Deployments, Services, ConfigMap, Secret, liveness/readiness probes wired to your existing endpoints, resource requests and limits
- `k8s/overlays/local/`: `ingress-nginx`, NodePort mapping
- Install KEDA via Helm; `ScaledObject` scaling **the gateway** (not Ollama), min 2 / max 8 — see [Kubernetes + KEDA](#8-kubernetes--keda) for what it scales *on*, which is not obvious
- Ollama as a single-replica Deployment. Give it a PVC, or it re-downloads ~2 GB of weights on every pod restart. If RAM is tight, run it on the host instead and point `OLLAMA_URL` at `host.docker.internal` (Docker Desktop) or the Docker bridge gateway IP.
- Load test with k6; record pods scaling up and back down

**Done when:** `kubectl apply -k k8s/overlays/local` brings up the whole system, and a load test visibly scales the gateway from 2 → 6 pods and back. Record this — it's your primary demo artifact.

**At the end of Phase 4 the project is complete.** Every capability in the resume bullet is real and demonstrable on a laptop. The appendix adds credibility, not capability.

---

## Appendix — cloud deployment (optional, deferred)

Not required. Do this only after Phase 4 works, and only if you want the "runs on infrastructure I don't control" claim. Budget 1–2 days.

### Choosing a provider
| Option | Control plane | Notes |
|---|---|---|
| **GKE (zonal Standard)** | Covered by a free-tier credit per billing account | Best documented; new accounts also get trial credits. Verify current free-tier terms — they change. |
| **DigitalOcean DOKS** | Free | Simplest UX, cheapest nodes, least ceremony |
| Azure AKS (free tier) | Free | Fine; weaker SLA, irrelevant here |
| ~~AWS EKS~~ | ~$0.10/hr, no free tier | Skip |

Nodes are the real cost either way — two small nodes run roughly $0.08/hour, so a 4-hour session is well under $1.

### Steps

1. **Provision** a 2-node cluster. Set a billing alert *first*.
2. **Secrets**: `kubectl create secret generic router-secrets --from-env-file=.env`. Same manifest reference as local, different source.
3. **Apply**: `kubectl apply -k k8s/overlays/cloud`. Same base, cloud overlay swaps ingress for a real LoadBalancer Service.
4. **Install KEDA** via Helm — identical command to Phase 4.
5. **Verify** the same `/stats` and `/healthz` endpoints against a public URL.
6. **Re-run the load test** against real infrastructure.
7. **Chaos check**: `kubectl delete pod` on a gateway mid-load and confirm zero dropped requests.
8. **Tear down**: `kubectl delete -k`, delete the cluster, verify in the billing console.
9. **Document the port** in `docs/cloud-port.md`: what broke, why, how you fixed it, what it cost.

### What differs from local

| Concern | Local (`kind`) | Cloud | Handled by |
|---|---|---|---|
| External access | `ingress-nginx` + port mapping | Cloud LoadBalancer / Ingress | One overlay file per environment |
| Image source | GHCR | GHCR | Identical — no difference at all |
| Secrets | K8s Secret from local `.env` | K8s Secret from cloud secret store | Same manifest, different source |
| Storage class | `standard` (kind default) | Provider default | Only matters for the Ollama PVC |
| Node count | Multi-node kind cluster | Real nodes | kind multi-node makes scheduling behave realistically |

Everything in that table is isolated into `k8s/overlays/local/` and `k8s/overlays/cloud/`, with the shared majority in `k8s/base/`. That structure *is* the portability work.

### What will probably break (budget an hour for each)
- **Architecture mismatch** — if multi-arch builds weren't set up in Phase 3, `exec format error` on x86 nodes
- **Image pull auth** — GHCR packages default to private; make them public or add an `imagePullSecret`
- **Resource limits too tight** — pods that fit a generous local machine get OOMKilled on small cloud nodes
- **Ollama** — a 3B model on a small cloud node is slow. Either accept it, size up one node, or run the demo in mock mode for that tier
- **LoadBalancer provisioning** — takes a few minutes and can silently fail on quota; `kubectl describe svc` tells you why

### Security, non-optional
The moment the gateway has a public IP, your Anthropic key is behind an internet-facing endpoint, and scanners find open endpoints fast. The Phase 3 API key check **and rate limit** are what stand between you and someone else's inference bill. Verify both are enforced before exposing anything, and keep the cluster up only as long as you need it.

---

## Component deep-dives

### 1. Gateway
FastAPI. Per-request order: cache check → (miss) classify → select tier → guardrail check → dispatch → cache write → return. Every step logged with a shared request id. Maintains an in-flight counter for `/stats` and, later, KEDA. Stateless by design, which is what makes horizontal scaling trivial and is worth being able to explain.

### 2. Classifier
- **v1 rules:** prompt token length, code-fence presence, sentence count, question marks, keyword list ("explain", "step by step", "write a function", "prove", "summarize").
- **v2 logistic regression:** baseline. Cheap, interpretable, and the thing you compare against.
- **v3 XGBoost:** same hand-crafted features, not embeddings — you want to point at a feature importance chart and explain exactly why a prompt routed where it did.
- **Labels by outcome**, as described in Phase 3. Not from ShareGPT or LMSYS-Chat-1M — those are conversation logs with no complexity ground truth, so you'd end up inventing the same labeling heuristic anyway, just with more indirection and less relevance to your actual tiers.

### 3. Model tiers
Common interface: `async def complete(prompt: str) -> TierResponse`, where `TierResponse` carries text, token counts, latency, and estimated cost. Mock mode is implemented once at this interface, not per-tier.

### 4. Redis cache

Cache-aside. The key has three parts:

```
cache:v1:{mock|live}:{sha256(normalized_prompt)}
```

- **`v1`** — a schema version, so changing the stored value's shape doesn't serve garbage from the old format.
- **`mock|live`** — the mode namespace, and this one is not optional. Load tests are supposed to run with `MOCK_TIERS=true`; without the namespace every canned mock response lands in the same keyspace real requests read from, and your demo serves `"mock response for tier local"` out of cache. Prompt-only keys make that a certainty, not a risk.
- **normalized prompt** — lowercased, whitespace-collapsed, then SHA-256.

Store the producing tier alongside the response:

```json
{"text": "...", "tier": "local", "created_at": "..."}
```

so `/stats` can report "hit, originally served by local," and so a minimum-tier policy is possible later without a cache wipe.

**TTL only, no invalidation logic.** A cached completion has no upstream source of truth that can change underneath it, so expiry is sufficient — and "I chose TTL because there's nothing to invalidate against" is a better answer than a half-built invalidation scheme.

**A known tradeoff, stated rather than hidden:** because the key is prompt-only and the cache is checked before classification, the first tier to answer a prompt answers it forever. A prompt first handled by the 3B local model keeps returning that answer even if the classifier would now route it to Sonnet. That is acceptable for this system, but it is a real consequence of the design, not a free lunch — which is exactly why the producing tier is recorded.

### 5. Load balancer — the piece to get right
Own service, fronting N gateway replicas.

- **Round-robin:** counter mod N.
- **Least-connections:** track in-flight count per replica; dispatch to the lowest.
- **Health checks:** poll `/readyz`; mark down, skip in rotation, keep polling.
- **Circuit breaker:** consecutive-failure counter per replica; trip at 3, cool down 30s, half-open probe before restoring.
- **Consistent hashing:** deliberately excluded. It solves session affinity; every request here is independent. Adding it would be solving a problem this system doesn't have.

~150 lines, no library. This is the most whiteboard-worthy code in the project — know it cold.

**"But doesn't Kubernetes make this redundant?"** In Phase 4 a Service does the load balancing, yes — and that's the point of having built it first. You can explain precisely what the Service abstraction does because you implemented it. Better still, your version does something a plain Service *doesn't*: a Kubernetes Service has no circuit breaker — that's what a service mesh like Istio or Linkerd adds. Knowing exactly where the platform primitive stops and where you'd reach for a mesh is a genuinely senior-sounding distinction, and you'll have earned it.

**Replica discovery.** The balancer resolves a DNS name to every address behind it and reconciles its replica set each health-check cycle, adding and removing replicas as they appear and vanish. Docker's embedded DNS returns all container IPs for a service name, so scaling the gateway needs no configuration change. `GATEWAY_REPLICAS` overrides discovery with a fixed list.

**What this means for Phase 4.** The same mechanism carries over to Kubernetes, but only against a *headless* Service — a normal ClusterIP Service resolves to a single virtual IP, which would collapse the pool to one entry. That is a one-line manifest change rather than the rewrite an env-var-based balancer would have needed. Still worth stating plainly: in Phase 4 the Service itself also load-balances, so the question of whether traffic flows through this balancer or straight to the Service is a deployment choice, and only the balancer path has a circuit breaker.

### 6. Capability guardrails
One manifest per tier:

```yaml
# app/manifests/local.yaml
tier: local
allowed_capabilities:
  - text_generation
denied_capabilities:
  - web_search
  - code_execution
  - file_access
```

A FastAPI dependency loads the selected tier's manifest, checks the request's requested capabilities against it before dispatch, and logs violations (tier, capability, timestamp, request id). Write a small adversarial test set and report the false-positive/false-negative rate — that number is the interesting part.

### 7. CI/CD
On push: `ruff` → `pytest` (mock mode) → `docker buildx` multi-arch → push to GHCR tagged with the commit SHA. No repository secrets required, which is itself a design win worth pointing out.

### 8. Kubernetes + KEDA

Kustomize base + two overlays. KEDA `ScaledObject` on the gateway Deployment, min 2 / max 8.

**What it scales on, and why the obvious answer is wrong.** The intuitive choice is KEDA's Redis scaler on list length. But the gateway is a *synchronous* service — a client POSTs `/query` and blocks until the response. Nothing is ever enqueued in Redis, so there is no list for `LLEN` to measure. A `ScaledObject` pointed at a Redis list would sit at zero forever and never scale.

Two workable options:

1. **`metrics-api` scaler against the gateway's in-flight counter** — recommended. The gateway already tracks in-flight requests (Phase 1); expose them at `GET /scale-metric` returning `{"in_flight": N}` and point KEDA at it. No Prometheus, no leak.
2. **`LPUSH` a token on request entry, `LREM` on completion** — works with the Redis scaler, but a pod that dies mid-request leaks its token permanently, since Redis lists have no per-element TTL. The metric then ratchets upward and never scales back down. Needs a sweeper to be safe.

Either way, note honestly in your README that concurrency is a *proxy* for latency pressure — the literal p95-latency version needs a `/metrics` endpoint and KEDA's Prometheus scaler, which is in [Stretch ideas](#stretch-ideas).

---

## Repo structure

```
AI-Router/
  app/
    main.py                    # FastAPI gateway
    load_balancer.py           # standalone LB service
    classifier/
      rules.py
      features.py
      train.py                 # logreg + xgboost, writes both models
    tiers/
      base.py                  # common interface + mock mode
      local_ollama.py
      haiku.py
      sonnet.py
    cache.py
    guardrails.py
    manifests/
      local.yaml
      haiku.yaml
      sonnet.yaml
  tests/
    test_load_balancer.py
    test_circuit_breaker.py
    test_cache.py
    test_guardrails.py
  data/
    labeled_prompts.csv
    adversarial_capability_tests.yaml
  k8s/
    base/                      # the shared majority
    overlays/
      local/                   # kind: ingress-nginx, NodePort
      cloud/                   # LoadBalancer service (appendix)
    keda/scaledobject.yaml
  load/
    k6-script.js
  docs/
    architecture.md
    cloud-port.md              # written only if the appendix is done
    metrics.md
  .github/workflows/ci.yml
  docker-compose.yml
  kind-config.yaml
  Makefile                     # make dev / make test / make kind / make demo
  requirements.txt
  .env.example
  README.md
```

---

## Metrics to log

Track from Phase 1 — these are both your debugging surface and your portfolio evidence.

**Routing and cost:** per-tier request counts, token cost by tier, estimated savings vs. routing everything to Sonnet, classifier accuracy (rules vs. logreg vs. XGBoost on a held-out set, with the sample size stated).

**Cache:** hit rate, latency on hit vs. miss, cost avoided, and the distribution of *originating* tiers among hits.

**Load balancer:** request distribution across replicas, latency under simulated replica failure, circuit breaker trip and recovery events.

**Scaling:** time from load spike to new pod ready, pod count over time during the load test, p50/p95/p99 latency under load.

**Guardrails:** violation rate against your adversarial test set, false positives and false negatives.

---

## Budget

| Item | Cost |
|---|---|
| Outcome-based labeling run (~300 prompts × 3 tiers) | ~$2.50 |
| Development and manual testing | ~$3–5 (less if you default to mock mode) |
| Docker, Redis, Ollama, kind, KEDA, GitHub Actions, GHCR | $0 |
| **Total through Phase 4** | **~$6–8** |
| Cloud cluster, appendix only (2 nodes, ~4–6 hours) | ~$1–3 |

Everything through Phase 4 — including autoscaling — costs nothing but the API spend. The cluster is the only line item that needs a credit card, and it's optional.

Two ways to blow the budget, both avoidable: a retry loop hammering Sonnet (set a hard console spend limit), and load-testing against paid tiers (always use `MOCK_TIERS=true` for load tests — that's what it's for).

---

## Hardware requirements

| RAM | Verdict |
|---|---|
| 16 GB+ | Comfortable throughout, including Phase 4 |
| 8 GB | Fine through Phase 3. For Phase 4, run Ollama on the host rather than in-cluster, use a 1B model, and shut Compose down before starting kind |

To reach a host-run Ollama from inside kind, point `OLLAMA_URL` at `host.docker.internal` (Docker Desktop) or the Docker bridge gateway IP. Note that `extraHosts` is a Docker Compose field, not a kind one — kind's node config offers `extraMounts` and `extraPortMappings`.

The only real memory consumer is Ollama's model weights. Gateway pods are ~150 MB each, which is exactly why the scaling target is the gateway. Apple Silicon runs local inference noticeably better than older Intel hardware — and remember rule 4 of the portability contract if you're on it.

---

## Explicitly out of scope (and why)

| Idea | Why |
|---|---|
| Multi-agent orchestration (LangGraph, Plan-and-Execute) | Different problem — agentic tool use, not routing. Doubles scope, adds nothing to these claims. Build it as a separate project if you want it. |
| Hand-built Redis clone | Real Redis is the skill being tested. A homemade KV store is a different, harder project wearing this one's clothes. |
| Consistent hashing | Solves session affinity; this system has no sessions. |
| LangSmith / external tracing | Only pays off with LangGraph. Structured JSON logs cover the same ground with no dependency. |
| Training on ShareGPT / LMSYS-Chat-1M | No complexity labels in them; you'd derive labels yourself regardless, and outcome-based labels against your actual tiers are strictly more useful. |
| Service mesh (Istio, Linkerd) | Correct answer to "where does circuit breaking live in production" — and a much better thing to *discuss* than to install here. |

---

## Interview talking points

- **Why build your own load balancer instead of using nginx?** To understand the algorithms rather than the config syntax. And it does something a plain Kubernetes Service doesn't — circuit breaking, which in production is a service mesh concern.
- **Why check the cache before classifying?** A hit costs zero model calls *and* zero classifier calls. Checking after would waste the classifier on every repeat.
- **What does that cost you?** The first tier to answer a prompt answers it forever. I record the producing tier in the cache entry so the tradeoff is measurable rather than invisible, and so a minimum-tier policy is possible without a cache wipe.
- **How did you build your training set?** Ran every prompt through all three tiers and labeled each with the cheapest tier that gave an acceptable answer. The labels reflect real capability boundaries, not my guesses about them.
- **Why XGBoost over logistic regression?** I trained both — here are both confusion matrices, and here's the held-out sample size, which is small enough that I'd not over-claim a narrow win. (Answer honestly from your own numbers. If logreg won, say so; that's a better answer than a rationalization.)
- **Is the autoscaling actually latency-driven?** It scales on in-flight concurrency, which is a proxy for latency pressure. The obvious approach — KEDA's Redis list scaler — doesn't work here at all, because the gateway is synchronous and nothing is ever enqueued. The literal p95 version needs a Prometheus scaler; I know the change and the tradeoff.
- **Why local Kubernetes?** Because Kubernetes isn't cloud. `kind` gave me real manifests, real probes, and real KEDA autoscaling on a laptop for free — so the only thing a cloud deployment would add is proving it runs on hardware I don't own.
- **Why not run it 24/7?** It's a portfolio project, not a service. The repo, the manifests, and the recording are the artifact.

---

## Stretch ideas

Only after Phase 4 is done.

- The cloud appendix, if you want the "runs on infrastructure I don't control" claim
- Headless-Service DNS discovery for the load balancer, so one balancer serves both the Compose and Kubernetes paths and the circuit breaker survives into the cluster
- `/metrics` in Prometheus format + KEDA Prometheus scaler, making the autoscaling literally p95-latency-driven
- Multi-turn conversation support — which would finally justify consistent hashing for session affinity
- Canary routing: shift a percentage of traffic to a new tier or model version before switching fully
- Semantic caching: embed prompts and serve near-matches, not just exact matches (interesting failure modes — worth doing carefully or not at all)
- Grafana dashboard over the structured logs, for a better demo screenshot
