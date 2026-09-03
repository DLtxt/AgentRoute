# AI-Router

A cost-aware LLM request router. It classifies each incoming query and dispatches it to the cheapest model tier that can handle it, with response caching, a hand-built load balancer, capability guardrails, authentication, and rate limiting.

Everything runs locally, on Docker Compose or on a local Kubernetes cluster with autoscaling. See [`plan.md`](plan.md) for what is implemented.

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

`train.py` prints the majority-class baseline and the achievable ceiling next to each model's accuracy, warns when a class has no held-out examples, and compares the two models against the standard error rather than a fixed threshold.

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

### Tear down

```bash
make kind-down
```

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
