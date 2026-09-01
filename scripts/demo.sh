#!/usr/bin/env bash
# Ring 0 demo: the same prompt twice. The second is a cache hit.
set -euo pipefail
HOST="${HOST:-http://localhost:8000}"
PROMPT="${PROMPT:-Explain why quicksort is O(n log n) on average, step by step.}"

command -v jq >/dev/null || { echo "This demo needs jq: brew install jq"; exit 1; }

echo "== waiting for readiness =="
for _ in $(seq 1 30); do
  curl -sf "$HOST/readyz" >/dev/null && break
  sleep 1
done
curl -s "$HOST/readyz" | jq .

for n in 1 2; do
  echo
  echo "== request $n =="
  curl -s -X POST "$HOST/query" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg p "$PROMPT" '{prompt:$p}')" \
  | jq '{tier, cached, reason, latency_ms, cost_usd}'
done

echo
echo "== stats =="
curl -s "$HOST/stats" | jq '{cache, cost}'
