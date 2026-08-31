# AI Router & Autoscaling Platform — Two-Part Project Plan

A cost-aware LLM request router that classifies incoming queries and dispatches each to the cheapest model tier that can handle it, with response caching, a hand-built load balancer, capability guardrails, and autoscaling.

Built in two parts: **Part 1 runs the complete system locally, on Kubernetes, with no cloud account required.** **Part 2 deploys that same system to a real cloud cluster** — same manifests, same images, same code.

---

## Contents
- [The two-part structure](#the-two-part-structure)
- [What "identical to cloud" actually means](#what-identical-to-cloud-actually-means)
- [Goals](#goals)
- [Architecture](#architecture)
- [The portability contract](#the-portability-contract)
- [Tech stack](#tech-stack)
- [Day 0 setup](#day-0-setup)
- [PART 1 — Locally complete (weeks 1–4)](#part-1--locally-complete-weeks-14)
- [PART 2 — Cloud deployment (week 5)](#part-2--cloud-deployment-week-5)
- [Component deep-dives](#component-deep-dives)
- [Repo structure](#repo-structure)
- [Metrics to log](#metrics-to-log)
- [Budget](#budget)
- [Hardware requirements](#hardware-requirements)
- [Explicitly out of scope](#explicitly-out-of-scope-and-why)
- [Interview talking points](#interview-talking-points)
- [Stretch ideas](#stretch-ideas)

---

## The two-part structure

**Part 1 (weeks 1–4): everything works locally, end to end, on Kubernetes.**
Anyone can clone the repo and have the full system running — routing, caching, load balancing, guardrails, autoscaling, CI — without a cloud account and without an API key (mock mode covers the paid tiers). This is a complete, defensible, demoable project on its own. If you stop here, you still have something worth putting on a resume.

**Part 2 (week 5, realistically 1–2 days): the same system on a real cloud cluster.**
Because Part 1 already produced Kubernetes manifests and portable images, Part 2 is provisioning a cluster, applying the same YAML, wiring real secrets and real ingress, and capturing the scaling demo on real infrastructure. Then tearing it down.

**Why this split works:** the expensive, slow, easy-to-get-wrong parts (routing logic, ML, load balancing, K8s concepts) get debugged locally where iteration is instant and free. Cloud is reserved for the one thing that genuinely requires it — proving it runs on infrastructure you don't control. This is also how real teams work: local dev loop, cloud deployment target.

**Why Part 1 ends on Kubernetes, not Docker Compose:** if Part 1 stopped at Compose, Part 2 would mean learning Kubernetes *and* debugging cloud-specific issues simultaneously, on a metered clock. Ending Part 1 on `kind` means every Kubernetes concept is already understood and every manifest already works before a cloud bill starts. Compose stays in the repo as the fast inner-loop dev environment — both are used, for different things, exactly as they would be on a real team.

---

## What "identical to cloud" actually means

Honest accounting of what carries over unchanged versus what differs:

**Identical (the ~90%):**
- All application code
- All container images (multi-arch, pulled from the same registry)
- All Kubernetes manifests — Deployments, Services, ConfigMaps, Secrets, probes, resource limits
- KEDA `ScaledObject` and scaling behavior
- The env-var configuration contract
- Health/readiness probe endpoints and semantics

**Differs (the ~10%, all anticipated and one-line):**

| Concern | Local (`kind`) | Cloud | Handled by |
|---|---|---|---|
| External access | `ingress-nginx` + port mapping | Cloud LoadBalancer / Ingress | One overlay file per environment |
| Image source | Pull from GHCR (same as cloud) | Pull from GHCR | Use GHCR in both from week 3 — no difference at all |
| Secrets | K8s Secret from local `.env` | K8s Secret from cloud secret store or `kubectl create secret` | Same manifest, different source |
| Storage class | `standard` (kind default) | Provider default | Redis runs without persistence here, so it doesn't come up |
| Node count | Multi-node kind cluster (2–3 nodes) | Real nodes | kind multi-node config makes scheduling behave realistically |

Everything in the "differs" column is isolated into `k8s/overlays/local/` and `k8s/overlays/cloud/`, with the shared 90% in `k8s/base/`. That structure *is* the portability work.

---

## Goals
- A system that runs end-to-end locally with one command, and on a cloud cluster with one more.
- Learn hands-on: load-balancing algorithms, cache-aside design, ML classification with hand-crafted features, Kubernetes primitives and autoscaling, CI/CD, capability-based authorization, and the local→cloud port itself.
- Stay under $20 total and inside ~5 weeks part-time.

---

## Architecture

### Request path (identical in both parts)

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

1. **Cache check precedes classification.** A repeat prompt costs a hash lookup — not a classifier call plus a model call. Free to build, strictly better.
2. **The load balancer fronts the gateway, not the model tiers.** Haiku and Sonnet are hosted APIs; Anthropic already load-balances those. The only thing you host, and therefore the only thing you can balance, is your own gateway.

### Deployment topology

| Component | Replicas | Scales? | Why |
|---|---|---|---|
| Gateway | 2 → N | **Yes**, via KEDA | Stateless, ~150 MB per pod, and it's what's actually under concurrent pressure |
| Ollama | 1 | **No** | Each replica loads its own copy of the model weights — scaling this is a RAM disaster and buys nothing |
| Redis | 1 | No | Shared cache; scaling it would fragment the cache |
| Custom load balancer | 1 | No | Entry point (Part 1 Compose mode; see note in Component 5) |

---

## The portability contract

Follow these from day one and Part 2 is a day. Retrofit them later and Part 2 is a week.

1. **No hardcoded hostnames.** Every service address comes from an env var (`REDIS_URL`, `OLLAMA_URL`, `GATEWAY_REPLICAS`). Never `localhost` in application code.
2. **All config via environment.** No config files read from host paths, no absolute paths, no assumptions about the working directory.
3. **Secrets only via env vars.** Never in a ConfigMap, never in an image, never committed. `.env` locally → K8s Secret in both clusters.
4. **Build multi-arch images.** `docker buildx build --platform linux/amd64,linux/arm64`. If you're on Apple Silicon and skip this, your images will not run on standard x86 cloud nodes, and the error message won't make it obvious why.
5. **One registry everywhere.** Push to GHCR (free for public repos) from week 3. Both clusters pull the same image by digest — no `kind load`, no drift.
6. **Real health endpoints.** `/healthz` (liveness — is the process alive?) and `/readyz` (readiness — can it serve traffic? Redis reachable?). Your custom load balancer's health check and Kubernetes' readiness probe consume the same endpoint. That's not a coincidence; it's the same concept.
7. **Mock mode.** `MOCK_TIERS=true` stubs the paid tiers with canned responses and realistic per-tier delays. Makes CI runnable without secrets, load tests free, and the repo runnable by a reviewer with no API key.
8. **Resource requests and limits on every pod.** Required for meaningful scheduling, and forces you to actually know what your services consume.

---

## Tech stack

### Accounts
| What | For | Cost |
|---|---|---|
| Anthropic API key (console.anthropic.com) | Haiku + Sonnet tiers | ~$5–8 total; set a hard spend limit before you start |
| GitHub | Repo, Actions, GHCR image registry | Free (public repo) |
| Google Cloud **or** DigitalOcean | Part 2 only | ~$1–3, ephemeral |

### Local installs
| Tool | Purpose | Part |
|---|---|---|
| Docker Desktop / Engine + Compose | Container runtime, inner dev loop | 1 |
| Ollama | Local model tier | 1 |
| Python 3.11+ | `fastapi`, `uvicorn`, `httpx`, `anthropic`, `redis`, `pyyaml`, `scikit-learn`, `xgboost`, `pandas`, `python-dotenv`, `pytest`, `ruff` | 1 |
| kind | Local Kubernetes cluster | 1 |
| kubectl | Cluster CLI | 1, 2 |
| Helm | Installs KEDA | 1, 2 |
| k6 or hey | Load generation | 1, 2 |
| gcloud CLI **or** doctl | Cloud cluster provisioning | 2 |

### Models
| Tier | Model | How | Cost per 1M tokens (Aug 2026) |
|---|---|---|---|
| Local | Llama 3.2 3B or Phi-4-mini (Q4) | `ollama pull llama3.2:3b` | $0 |
| Mid | Claude Haiku 4.5 | `claude-haiku-4-5-20251001` | $1 in / $5 out |
| High | Claude Sonnet 5 | `claude-sonnet-5` | $2 in / $10 out (intro, through Aug 31 2026; $3/$15 after) |

Check `ollama.com/library` for the current best small-model tag before pulling, and `claude.com/pricing` before building your cost chart — both move.

---

## Day 0 setup

```bash
mkdir ai-router && cd ai-router
git init
python -m venv venv && source venv/bin/activate
pip install fastapi uvicorn httpx anthropic redis pyyaml scikit-learn xgboost pandas python-dotenv pytest ruff
ollama pull llama3.2:3b
cp .env.example .env    # then add ANTHROPIC_API_KEY
```

Verify Ollama is reachable before writing any router code:
```bash
curl http://localhost:11434/api/generate -d '{"model":"llama3.2:3b","prompt":"hi","stream":false}'
```

---

## PART 1 — Locally complete (weeks 1–4)

### Week 1 — the pipeline, on Compose

- FastAPI gateway with `POST /query`, `GET /healthz`, `GET /readyz`, `GET /stats`
- Rule-based classifier: prompt length, code-fence regex, sentence count, keyword flags
- Three tier handlers behind a common interface (Ollama via REST, Haiku and Sonnet via the Anthropic SDK)
- **Mock mode** (`MOCK_TIERS=true`) with per-tier canned latency — build this in week 1, not later; everything downstream depends on it
- Redis cache-aside, key = SHA-256 of the normalized prompt, checked *before* classification
- Structured JSON logging: request id, cache hit/miss, tier chosen, latency, token counts, cost estimate
- `docker-compose.yml`: gateway, redis, ollama

**Done when:** `docker compose up` then two identical curls — the second returns in single-digit milliseconds. That contrast is your first demo moment.

### Week 2 — load balancer and guardrails

- Extract the load balancer into its own service (`app/load_balancer.py`, own container), fronting 2+ gateway replicas
- Round-robin first. Then least-connections. Then health checking against `/readyz`. Then a circuit breaker (trip after 3 consecutive failures, 30s cooldown, half-open retry)
- Per-tier YAML capability manifests + a FastAPI dependency enforcing them before dispatch, logging every violation
- Compose now runs: load balancer, gateway ×3, redis, ollama

**Done when:** you can `docker compose stop` one gateway replica mid-load-test and watch traffic redistribute with zero failed requests, and `/stats` shows the per-replica distribution.

### Week 3 — ML, tests, CI

- **Build the labeled dataset by outcome, not intuition.** Collect ~300 prompts, run each through all three tiers, compare outputs, label each with *the cheapest tier that produced an acceptable answer*. This costs about $2 and it's the difference between a real dataset and a model trained to predict your own guesses.
- Train logistic regression as a baseline, then XGBoost. Same hand-crafted features. Report both confusion matrices — having the baseline is what makes the XGBoost choice defensible instead of decorative.
- Keep classifier selection behind `CLASSIFIER=rules|logreg|xgb` so all three are A/B-able at runtime
- **Tests worth having** (all runnable in mock mode, no secrets):
  - load balancer distributes evenly across replicas over N requests
  - circuit breaker trips after consecutive failures and recovers after cooldown
  - cache returns on hit, populates on miss, and expires on TTL
  - guardrail blocks a denied capability and logs the violation
- GitHub Actions: `ruff` + `pytest` (mock mode, no API key needed) + `docker buildx` multi-arch build + push to GHCR
- API key header check on the gateway

**Done when:** CI is green on a fresh clone with zero repository secrets configured.

### Week 4 — local Kubernetes

- `kind` cluster, 3 nodes (`kind-config.yaml`), so scheduling behaves realistically
- `k8s/base/`: Deployments, Services, ConfigMap, Secret, liveness/readiness probes wired to your existing endpoints, resource requests and limits
- `k8s/overlays/local/`: `ingress-nginx`, NodePort mapping
- Install KEDA via Helm; `ScaledObject` scaling **the gateway** (not Ollama) on Redis queue depth, min 2 / max 8
- Ollama as a single-replica Deployment (or on the host via `extraHosts` if RAM is tight)
- Load test with k6; record pods scaling up and back down

**Done when:** `kubectl apply -k k8s/overlays/local` brings up the whole system, and a load test visibly scales the gateway from 2 → 6 pods and back. Record this — it's your primary demo artifact.

**At the end of Part 1 you have a complete, self-contained project.** Everything in the resume bullet is real and demonstrable. Part 2 adds credibility, not capability.

---

## PART 2 — Cloud deployment (week 5)

Budget 1–2 days. The goal is proving it runs on infrastructure you don't control, and learning what breaks in the port.

### Choosing a provider
| Option | Control plane | Notes |
|---|---|---|
| **GKE (zonal Standard)** | Covered by a $74.40/month free-tier credit per billing account | Best documented; new accounts also get $300 in trial credits |
| **DigitalOcean DOKS** | Free | Simplest UX, cheapest nodes, least ceremony |
| Azure AKS (free tier) | Free | Fine; weaker SLA, irrelevant here |
| ~~AWS EKS~~ | $0.10/hr (~$73/mo), no free tier | Skip |

Nodes are the real cost either way — two small nodes run roughly $0.08/hour, so a 4-hour session is well under $1.

### Steps

1. **Provision** a 2-node cluster. Set a billing alert *first*.
2. **Secrets**: `kubectl create secret generic router-secrets --from-env-file=.env`. Same manifest reference as local, different source.
3. **Apply**: `kubectl apply -k k8s/overlays/cloud`. Same base, cloud overlay swaps ingress for a real LoadBalancer Service.
4. **Install KEDA** via Helm — identical command to Part 1.
5. **Verify** the same `/stats` and `/healthz` endpoints against a public URL.
6. **Re-run the load test** against real infrastructure. Capture the scaling clip here — this version is the one worth showing.
7. **Chaos check**: `kubectl delete pod` on a gateway mid-load and confirm zero dropped requests.
8. **Tear down**: `kubectl delete -k`, delete the cluster, verify in the billing console.
9. **Document the port** in `docs/cloud-port.md`: what broke, why, how you fixed it, what it cost.

### What will probably break (budget an hour for each)
- **Architecture mismatch** — if multi-arch builds weren't set up in week 3, `exec format error` on x86 nodes
- **Image pull auth** — GHCR packages default to private; make them public or add an `imagePullSecret`
- **Resource limits too tight** — pods that fit a generous local machine get OOMKilled on small cloud nodes
- **Ollama** — a 3B model on a small cloud node is slow. Either accept it, size up one node, or run the demo in mock mode for that tier
- **LoadBalancer provisioning** — takes a few minutes and can silently fail on quota; `kubectl describe svc` tells you why

### Security, non-optional
The moment the gateway has a public IP, your Anthropic key is behind an internet-facing endpoint, and scanners find open endpoints fast. The week-3 API key check is what stands between you and someone else's inference bill. Verify it's enforced before exposing anything, and keep the cluster up only as long as you need it.

---

## Component deep-dives

### 1. Gateway
FastAPI. Per-request order: cache check → (miss) classify → select tier → guardrail check → dispatch → cache write → return. Every step logged with a shared request id. Stateless by design, which is what makes horizontal scaling trivial and is worth being able to explain.

### 2. Classifier
- **v1 rules:** prompt token length, code-fence presence, sentence count, question marks, keyword list ("explain", "step by step", "write a function", "prove", "summarize").
- **v2 logistic regression:** baseline. Cheap, interpretable, and the thing you compare against.
- **v3 XGBoost:** same hand-crafted features, not embeddings — you want to point at a feature importance chart and explain exactly why a prompt routed where it did.
- **Labels by outcome**, as described in week 3. Not from ShareGPT or LMSYS-Chat-1M — those are conversation logs with no complexity ground truth, so you'd end up inventing the same labeling heuristic anyway, just with more indirection and less relevance to your actual tiers.

### 3. Model tiers
Common interface: `async def complete(prompt: str) -> TierResponse`, where `TierResponse` carries text, token counts, latency, and estimated cost. Mock mode is implemented once at this interface, not per-tier.

### 4. Redis cache
- Cache-aside; key = SHA-256 of the normalized prompt (lowercased, whitespace-collapsed)
- TTL only, no invalidation logic. A cached completion has no upstream source of truth that can change underneath it, so expiry is sufficient — and "I chose TTL because there's nothing to invalidate against" is a better answer than a half-built invalidation scheme
- Key is prompt-only, not tier-scoped: a cached answer is valid regardless of which tier produced it
- Also holds the queue-depth counter KEDA scales on

### 5. Load balancer — the piece to get right
Own service, fronting N gateway replicas.

- **Round-robin:** counter mod N.
- **Least-connections:** track in-flight count per replica; dispatch to the lowest.
- **Health checks:** poll `/readyz`; mark down, skip in rotation, keep polling.
- **Circuit breaker:** consecutive-failure counter per replica; trip at 3, cool down 30s, half-open probe before restoring.
- **Consistent hashing:** deliberately excluded. It solves session affinity; every request here is independent. Adding it would be solving a problem this system doesn't have.

~150 lines, no library. This is the most whiteboard-worthy code in the project — know it cold.

**"But doesn't Kubernetes make this redundant?"** In Part 2 a Service does the load balancing, yes — and that's the point of having built it first. You can explain precisely what the Service abstraction does because you implemented it. Better still, your version does something a plain Service *doesn't*: a Kubernetes Service has no circuit breaker — that's what a service mesh like Istio or Linkerd adds. Knowing exactly where the platform primitive stops and where you'd reach for a mesh is a genuinely senior-sounding distinction, and you'll have earned it.

Keep the custom balancer as the Compose-mode entry point; both paths stay in the repo.

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
Kustomize base + two overlays. KEDA `ScaledObject` on the gateway Deployment, triggered by Redis list length. Note honestly in your README that queue depth is a *proxy* for latency pressure — the literal p95-latency version needs a `/metrics` endpoint and KEDA's Prometheus scaler, which is in [Stretch ideas](#stretch-ideas).

---

## Repo structure

```
ai-router/
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
    base/                      # the 90% that's identical
    overlays/
      local/                   # kind: ingress-nginx, NodePort
      cloud/                   # LoadBalancer service
    keda/scaledobject.yaml
  load/
    k6-script.js
  docs/
    architecture.md
    cloud-port.md              # written in Part 2
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

Track from week 1 — these are both your debugging surface and your portfolio evidence.

**Routing and cost:** per-tier request counts, token cost by tier, estimated savings vs. routing everything to Sonnet, classifier accuracy (rules vs. logreg vs. XGBoost on a held-out set).

**Cache:** hit rate, latency on hit vs. miss, cost avoided.

**Load balancer:** request distribution across replicas, latency under simulated replica failure, circuit breaker trip and recovery events.

**Scaling:** time from load spike to new pod ready, pod count over time during the load test, p50/p95/p99 latency under load.

**Guardrails:** violation rate against your adversarial test set, false positives and false negatives.

---

## Budget

| Item | Cost |
|---|---|
| Outcome-based labeling run (~300 prompts × 3 tiers) | ~$2 |
| Development and manual testing | ~$3–5 (less if you default to mock mode) |
| Cloud cluster, Part 2 (2 nodes, ~4–6 hours) | ~$1–3 |
| Docker, Redis, Ollama, kind, KEDA, GitHub Actions, GHCR | $0 |
| **Total** | **~$6–10** |

Two ways to blow it, both avoidable: a retry loop hammering Sonnet (set a hard console spend limit), and load-testing against paid tiers (always use `MOCK_TIERS=true` for load tests — that's what it's for).

---

## Hardware requirements

| RAM | Verdict |
|---|---|
| 16 GB+ | Comfortable throughout, including week 4 |
| 8 GB | Fine through week 3. For week 4, run Ollama on the host rather than in-cluster (`extraHosts` in kind-config), use a 1B model, and shut Compose down before starting kind |

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
- **How did you build your training set?** Ran every prompt through all three tiers and labeled each with the cheapest tier that gave an acceptable answer. The labels reflect real capability boundaries, not my guesses about them.
- **Why XGBoost over logistic regression?** I trained both — here are both confusion matrices. (Answer honestly from your own numbers. If logreg won, say so; that's a better answer than a rationalization.)
- **Is the autoscaling actually latency-driven?** It scales on queue depth, which is a proxy for latency pressure. The literal version needs a Prometheus scaler on p95 — I know the change and the tradeoff.
- **What broke when you moved to cloud?** Point at `docs/cloud-port.md`. This is the single most credible thing in the project, because everyone who has actually done it recognizes the failure modes.
- **Why local Kubernetes before cloud Kubernetes?** So the only new variable in the cloud step was the cloud itself. Debugging manifests locally is free and instant; debugging them on a metered cluster is neither.
- **Why not run it 24/7?** It's a portfolio project, not a service. I ran the cluster ephemerally, captured the evidence, and tore it down — the repo, the manifests, and the recording are the artifact.

---

## Stretch ideas

Only after both parts are done.

- `/metrics` in Prometheus format + KEDA Prometheus scaler, making the autoscaling literally p95-latency-driven
- Multi-turn conversation support — which would finally justify consistent hashing for session affinity
- Canary routing: shift a percentage of traffic to a new tier or model version before switching fully
- Semantic caching: embed prompts and serve near-matches, not just exact matches (interesting failure modes — worth doing carefully or not at all)
- Grafana dashboard over the structured logs, for a better demo screenshot
- Deploy the gateway across two node pools and compare scheduling behavior