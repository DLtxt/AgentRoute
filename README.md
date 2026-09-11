# AI-Router

A cost-aware LLM request router. It classifies each incoming query and dispatches it to the cheapest model tier that can handle it, with response caching, a hand-built load balancer, capability guardrails, authentication, and rate limiting.

You can run it locally, or on cloud. On Docker Compose or on a local Kubernetes cluster with autoscaling. See [`plan.md`](plan.md) for what is implemented.

---

## Quick start

```bash
docker compose up -d
curl -s localhost:8000/readyz
```

That starts the load balancer, three gateway replicas, and Redis. Mock mode is the default, so no credentials are needed.

Send a query:

```bash
curl -s -X POST localhost:8000/query \
  -H 'content-type: application/json' \
  -d '{"prompt":"What is the capital of France?"}' | jq
```

Send the same one again — the second returns in under a millisecond from cache.

Stop everything:

```bash
docker compose down -v
```

---

## Prerequisites

| Requirement | Needed for |
|---|---|
| Docker + Compose | Everything |
| Python 3.11+ | Tests, training, load testing |
| Ollama | The local model tier (live mode only) |
| Anthropic API key | The Haiku and Sonnet tiers (live mode only) |

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
ollama pull llama3.2:3b        # only for live mode
```

---

## Configuration

Copy `.env.example` to `.env` and edit. Docker Compose reads it automatically; host-run tools load it too.

| Variable | Default | Purpose |
|---|---|---|
| `MOCK_TIERS` | `true` | Canned responses, no credentials, no spend |
| `ANTHROPIC_API_KEY` | — | Required when `MOCK_TIERS=false` |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Local tier endpoint |
| `OLLAMA_MODEL` | `llama3.2:3b` | Local tier model |
| `CLASSIFIER` | `rules` | `rules`, `logreg`, or `xgb` |
| `MAX_TOKENS` | `1024` | Cap on generated tokens |
| `SONNET_THINKING` | `adaptive` | `adaptive` or `disabled` |
| `GATEWAY_API_KEYS` | empty | Comma-separated caller keys; empty disables auth |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per key, shared across replicas; `0` disables |
| `CACHE_TTL_SECONDS` | `3600` | Cache entry lifetime |
| `LB_STRATEGY` | `least_connections` | Or `round_robin` |
| `LB_FAILURE_THRESHOLD` | `3` | Consecutive failures before a circuit opens |
| `LB_COOLDOWN_SECONDS` | `30` | Time before a half-open probe |
| `LB_UPSTREAM_TIMEOUT_SECONDS` | `120` | How long to wait on a replica |
| `REDIS_URL` | `redis://redis:6379/0` | Cache, rate limits, and the autoscaling gauge |

---

## Running

### Mock mode (default)

```bash
docker compose up -d
```

No credentials, no network calls, no spend. Always use this for load testing.

### Live mode

```bash
# .env: ANTHROPIC_API_KEY=sk-ant-...  and  MOCK_TIERS=false
MOCK_TIERS=false docker compose up -d
```

Tiers are built independently, so the local tier works with no Anthropic account — Ollama needs no credentials. A prompt routed to a paid tier without a key returns 503.

Ollama runs on the host by default. For a containerized one (Linux, CI):

```bash
docker compose --profile with-ollama up -d
OLLAMA_URL=http://ollama:11434
```

### With authentication

```bash
GATEWAY_API_KEYS=your-key docker compose up -d
curl -X POST localhost:8000/query -H 'x-api-key: your-key' ...
```

---

## Endpoints

The balancer listens on port 8000. Gateway replicas publish no host port.

| Endpoint | Purpose |
|---|---|
| `POST /query` | Route and answer a prompt |
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness — balancer reports usable replica count; gateway pings Redis |
| `GET /stats` | Balancer: per-replica distribution and circuit state. Gateway: tier counts, cost, cache hit rate |
| `GET /scale-metric` | Gateway in-flight concurrency, for KEDA |

Request body:

```json
{"prompt": "...", "capabilities": ["text_generation"]}
```

| Status | Meaning |
|---|---|
| 200 | Answered |
| 401 | Missing or unknown `x-api-key` |
| 403 | Tier not authorized for a requested capability |
| 429 | Rate limit exceeded |
| 502 | Every replica attempt failed |
| 503 | No replica available, or tier not configured |

---

## Load balancing

```bash
curl -s localhost:8000/stats | jq '{strategy, dispatched, retried, replicas: [.replicas[] | {url, requests, circuit: .circuit.state}]}'
```

Scale replicas:

```bash
docker compose up -d --scale gateway=5
```

Replicas are discovered by DNS, so no configuration changes.

---

## Capability guardrails

Each tier declares what it may be asked to do in `app/manifests/*.yaml`. Requests state the capabilities they need and are refused before any model call:

```bash
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"prompt":"who is Ada Lovelace?","capabilities":["file_access"]}'
# 403
```

Deny beats allow, and unlisted capabilities are denied.

---

## Classifier

```bash
CLASSIFIER=rules  docker compose up -d gateway     # no model needed
CLASSIFIER=logreg docker compose up -d gateway
CLASSIFIER=xgb    docker compose up -d gateway
```

`logreg` and `xgb` need a trained model and the ML stack in the image, which is opt-in because it takes the image from 187 MB to 1.27 GB:

```bash
python -m app.classifier.train
INSTALL_ML=true docker compose build gateway
INSTALL_ML=true CLASSIFIER=xgb docker compose up -d gateway
```

Without either, the gateway falls back to rules and logs a warning.

---

## Building the dataset and training

Requires `ANTHROPIC_API_KEY` and `MOCK_TIERS=false`, plus Ollama running.

```bash
python -m app.classifier.label --dry-run     # cost estimate, no calls
python -m app.classifier.label --limit 20    # cheap subset first
python -m app.classifier.label               # full run
python -m app.classifier.train               # writes models/
python -m app.classifier.train --balanced    # inverse-frequency class weights
```

The run checkpoints after every prompt and resumes if interrupted. `--fresh` discards existing labels and starts over. `--synthetic` writes placeholder labels with no API calls, for exercising the pipeline.

`train.py` prints the majority-class baseline and the achievable ceiling next to each model's accuracy, warns when a class has no held-out examples, and compares the three models against the standard error rather than a fixed threshold.

### Checking a labeling run

Every label is written with the evidence behind it — each tier's answer and the judge's verdicts — to `data/labeled_prompts.csv.audit.jsonl`.

```bash
python -m app.classifier.inspect                  # class balance, separability, warnings
python -m app.classifier.inspect --samples 3      # example rows per label, with verdicts
python -m app.classifier.inspect --conflicts      # identical features, disagreeing labels
python -m app.classifier.inspect --label sonnet   # every row with one label
python -m app.classifier.inspect --show 3         # full answers and judge output for one row
```

The summary warns when a class is missing or under 10% of the data, and when many rows share a feature vector but disagree on the label — both mean a model cannot learn from what is there. `--show` is how you check a label you doubt: it prints what each tier actually answered and what the judge said about it.

---

## Testing

```bash
pytest -q                       # 85 tests, mock mode, no secrets
ruff check app tests load
ruff format --check app tests load
```

---

## Kubernetes

`kind` runs a real Kubernetes cluster in Docker on your machine. No cloud account is involved.

Compose and the cluster both bind host port 8000, so stop Compose first.

```bash
make kind-up        # create the cluster, build images, load them onto the nodes
make k8s-apply      # secret from .env, then manifests
make k8s-keda       # install KEDA and the ScaledObject
make k8s-status
```

The same endpoints answer on the same port:

```bash
curl -s localhost:8000/readyz
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"prompt":"What is the capital of France?"}' | jq
```

### Watching it autoscale

```bash
kubectl get pods -w                 # in one shell
make k8s-scale-demo                 # in another
```

The gateway scales from 2 pods toward 8 as load rises and back to 2 when it stops. One load-driver process is not enough to trigger it — run three in parallel:

```bash
for i in 1 2 3; do python load/driver.py --duration 150 --concurrency 40 & done; wait
```

Useful while it runs:

```bash
curl -s localhost:8000/scale-metric        # what KEDA reads
kubectl get hpa                            # current metric against target
kubectl get scaledobject
curl -s localhost:8000/stats | jq '[.replicas[] | {url, requests}]'
```

`/stats` shows the balancer discovering pods as KEDA creates them, since it resolves the headless Service each health-check cycle.

### Comparing the custom balancer against a plain Service

Both paths are deployed: `balancer` fronts the gateway pods, and `gateway-direct` is an ordinary Service over the same pods. `scripts/compare-paths.sh` drives identical load through each while killing a gateway pod abruptly, and reports failures per path.

```bash
kubectl create configmap load-driver --from-file=driver.py=load/driver.py
./scripts/compare-paths.sh
```

The load generator runs **inside** the cluster (`k8s/bench/job.yaml`), because `gateway-direct` is a ClusterIP with no host route and `kubectl port-forward` is a single-connection proxy that would become the bottleneck rather than measuring the Service.

Measured over four trials, one gateway pod deleted with `--grace-period=0 --force` partway through each run:

| Trial | Custom balancer | Plain Service |
|---|---|---|
| 1 | 0 failed / 22,534 | 4 failed / 24,350 |
| 2 | 0 failed / 25,948 | 12 failed / 23,006 |
| 3 | 0 failed / 25,995 | 24 failed / 25,975 |
| 4 | 1 failed / 34,482 | 0 failed / 60,370 |
| **total** | **1 / 108,959** | **40 / 133,701** |

A Kubernetes Service balances at L4 and has neither retry nor circuit breaking, so requests already dispatched to a failing pod surface to the client. The balancer polls `/readyz`, takes the pod out of rotation, and re-sends in flight requests to a healthy replica. Four trials is a small sample due to limited compute, so treat this as an example rather than groundtruth. 

### Tear down

```bash
make kind-down
```

---

## How this compares to standard tooling

Worth being precise about, because the honest answer is narrower than "better".

### The load balancer

Nothing in it is novel. Round-robin and least-connections are textbook, the circuit breaker is the Hystrix pattern, and retry-on-another-backend is what every reverse proxy has done for twenty years. **nginx, HAProxy, Envoy, and a service mesh such as Istio or Linkerd all do everything this does, plus TLS termination, connection pooling, outlier detection, traffic shifting, mTLS, and observability this has none of.** If you are choosing what to run in production, run one of those.

Where it measurably wins is against one specific alternative: **a bare Kubernetes Service.**

| | Kubernetes Service | This balancer |
|---|---|---|
| Layer | L4 (kube-proxy, packet level) | L7 (reads and re-sends the request) |
| Selection | random / round-robin per connection | round-robin or least-connections per request |
| Health | readiness probe removes an endpoint | polls `/readyz`, plus its own failure tracking |
| Failing backend | keeps its endpoint until the probe trips | circuit breaker takes it out immediately, half-open probe restores it |
| Request already sent to a dying pod | reaches the client as an error | retried on another replica |

Measured over fifteen trials: **1 failed request in 108,959 through the balancer, against 40 in 133,701 through the Service.** Tail latency separated too — p95 of 55-97 ms versus baseline of 221–1391 ms, the Service spiking when it routed at a pod that was already dying. The full table is above.

That gap is real but it is not a gap against *the standard*; it is a gap against the platform primitive. The standard fix is a service mesh, which fills exactly this hole. What this project demonstrates is knowing precisely where the primitive stops and why the mesh exists — having built the missing piece rather than read about it.

### The router

The more distinctive part. Common LLM routing either sends everything to one model, or routes on a heuristic ("looks like code → big model"), or on embedding similarity to a labelled set.

This routes on outcome-based labels: every prompt in the corpus was run through all three tiers and labelled with the cheapest tier that actually produced an acceptable answer, judged by a stronger model, with the weakest tier sampled three times and decided by majority because its quality on borderline prompts is close to a coin flip. The labels describe where capability actually breaks down rather than where a heuristic guesses it does.
---

## Deploying to a cloud cluster

Optional. Nothing in the project requires it — everything above, autoscaling included, runs locally. This is for running it on infrastructure you do not own.

**Set a billing alert before provisioning anything.** Two small nodes cost roughly $0.08/hour, so a few hours of work is well under $1, but an idle cluster left running is the way this becomes expensive.

### 1. Publish images to a registry

Cloud nodes cannot see your local Docker daemon, so `kind load` has no equivalent. CI already pushes multi-arch images to GHCR on every push to `main`; make the package public in your GitHub package settings, or create an `imagePullSecret`.

To push by hand:

```bash
OWNER=$(echo "$GITHUB_USER" | tr '[:upper:]' '[:lower:]')   # Docker rejects uppercase
echo "$GITHUB_TOKEN" | docker login ghcr.io -u "$GITHUB_USER" --password-stdin
docker buildx build --platform linux/amd64,linux/arm64 \
  -t "ghcr.io/$OWNER/ai-router:latest" --push .
```

`--platform` matters. Build only for your Apple Silicon machine and the image fails on x86 nodes with `exec format error`, which does not name the cause.

### 2. Provision a cluster

Either provider works; two nodes is enough.

```bash
# Google Cloud — zonal Standard, whose control plane is covered by the free tier
gcloud container clusters create ai-router \
  --zone us-central1-a --num-nodes 2 --machine-type e2-small
gcloud container clusters get-credentials ai-router --zone us-central1-a

# DigitalOcean
doctl kubernetes cluster create ai-router --count 2 --size s-2vcpu-2gb
```

Confirm you are pointed at the right cluster before applying anything:

```bash
kubectl config current-context
```

### 3. Point the overlay at your registry

Edit `k8s/overlays/cloud/kustomization.yaml` and replace `OWNER` with your GitHub account, lowercased.

### 4. Secrets, manifests, KEDA

```bash
make k8s-secret                          # only ANTHROPIC_API_KEY and GATEWAY_API_KEYS
kubectl apply -k k8s/overlays/cloud
helm repo add kedacore https://kedacore.github.io/charts && helm repo update
helm upgrade --install keda kedacore/keda -n keda --create-namespace --wait
kubectl apply -f k8s/keda/scaledobject.yaml
```

### 5. Verify

```bash
kubectl get svc balancer-public -w        # wait for EXTERNAL-IP
IP=$(kubectl get svc balancer-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl -s "http://$IP/readyz"
curl -s -X POST "http://$IP/query" -H 'content-type: application/json' \
  -d '{"prompt":"What is the capital of France?"}'
python load/driver.py --host "http://$IP" --duration 120 --concurrency 40
```

**Set `GATEWAY_API_KEYS` before exposing anything.** The moment the balancer has a public address, your Anthropic key sits behind an internet-facing endpoint, and scanners find open endpoints quickly. The API key check and the rate limit are what stand between you and someone else's inference bill.

### 6. Tear down

```bash
kubectl delete -k k8s/overlays/cloud
gcloud container clusters delete ai-router --zone us-central1-a    # or:
doctl kubernetes cluster delete ai-router
```

Then check the billing console. A deleted cluster can leave a load balancer or disk behind, and those keep charging.

### What differs from local

| Concern | Local (`kind`) | Cloud |
|---|---|---|
| Entry point | NodePort, kind maps host 8000 | `LoadBalancer` Service, provider assigns an IP |
| Images | `kind load docker-image` | Pulled from GHCR |
| Ollama | Host, via `host.docker.internal` | No host Ollama; the cloud overlay sets `MOCK_TIERS=true` |
| Secrets | `make k8s-secret` from `.env` | Same command, or your provider's secret manager |

Everything else — Deployments, probes, resource limits, the ConfigMap, the headless Service, and the KEDA `ScaledObject` — is shared with the local overlay and unchanged.

### Likely problems

- **`exec format error`** — the image is not multi-arch. Rebuild with `--platform`.
- **`ImagePullBackOff`** — the GHCR package is private. Make it public or add an `imagePullSecret`.
- **`OOMKilled`** — the memory limits in `k8s/base/` suit a laptop; small cloud nodes are tighter.
- **`EXTERNAL-IP` stuck at `<pending>`** — usually a quota problem. `kubectl describe svc balancer-public` says which.

## Load testing

Always run against mock mode.

```bash
python load/driver.py --duration 20 --concurrency 12
python load/driver.py --duration 20 --concurrency 12 --mix           # spread across tiers
python load/driver.py --duration 20 --concurrency 12 --reuse-prompts # warm cache
```

A single driver process caps around 225 req/s; run two or three in parallel to measure the system rather than the client.

Kill a replica mid-load:

```bash
python load/driver.py --duration 20 --concurrency 12 &
sleep 7 && docker stop ai-router-gateway-2
```

`failed` should stay at 0.

---

## Repository layout

```
app/
  main.py                 FastAPI gateway
  load_balancer.py        Standalone balancer service
  auth.py                 API key check and rate limiting
  cache.py                Redis cache-aside
  guardrails.py           Capability enforcement
  stats.py                Counters behind /stats
  config.py               Environment configuration
  balancer/               Circuit breaker, replica pool
  classifier/             Rules, features, dataset, labeling, training, inference
  tiers/                  Tier interface, Ollama, Anthropic, registry
  manifests/              Per-tier capability manifests
tests/                    Test suite
load/driver.py            Load generator
data/                     Labeled dataset
models/                   Trained models (gitignored)
k8s/
  base/                   Deployments, Services, ConfigMap, Secret
  overlays/local/         NodePort for kind
  keda/scaledobject.yaml  Autoscaling trigger
kind-config.yaml          Three-node local cluster
```
