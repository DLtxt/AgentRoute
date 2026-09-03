#!/usr/bin/env bash
# Measure the custom balancer against a plain Kubernetes Service under identical
# load, killing a gateway pod abruptly partway through each run.
#
# The kill is --grace-period=0 --force on purpose. A graceful shutdown gives the
# endpoints controller time to withdraw the pod before it stops answering, and
# then both paths drop nothing -- which measures the shutdown sequence, not the
# proxy. Abrupt termination is the failure a circuit breaker and retry exist for.
set -uo pipefail
cd "$(dirname "$0")/.."

run_path() {
  local name="$1" target="$2"
  kubectl delete job pathbench --ignore-not-found >/dev/null 2>&1
  sleep 3
  sed "s|TARGET|$target|" k8s/bench/job.yaml | kubectl apply -f - >/dev/null

  # Let load build, then kill a gateway pod outright.
  sleep 25
  local victim
  victim=$(kubectl get pods -l app=gateway -o name | head -1)
  kubectl delete "$victim" --grace-period=0 --force >/dev/null 2>&1
  echo "    killed $victim mid-run"

  # A frozen backend makes requests hang for the driver's full timeout, so the
  # job can outrun a short deadline; wait generously.
  kubectl wait --for=condition=complete job/pathbench --timeout=400s >/dev/null 2>&1 \
    || kubectl wait --for=condition=failed job/pathbench --timeout=10s >/dev/null 2>&1
  sleep 3

  # By label, not `logs job/...`: that form reads a single pod and silently
  # discards the other two, which reported every run as zero requests.
  local logs
  logs=$(kubectl logs -l job-name=pathbench --tail=-1 --prefix=false 2>/dev/null)
  python3 - "$name" <<PY
import re, sys
logs = """$logs"""
reqs = [int(m) for m in re.findall(r'^requests\s+(\d+)', logs, re.M)]
ok   = [int(m) for m in re.findall(r'^ok\s+(\d+)', logs, re.M)]
bad  = [int(m) for m in re.findall(r'^failed\s+(\d+)', logs, re.M)]
p95  = [int(m) for m in re.findall(r'p95 (\d+)ms', logs)]
print(f"    {sys.argv[1]:<22} requests={sum(reqs):<6} ok={sum(ok):<6} FAILED={sum(bad):<4} p95max={max(p95) if p95 else '-'}ms")
PY
  # Let the deployment restore its floor before the next run.
  kubectl wait --for=condition=available deploy/gateway --timeout=120s >/dev/null 2>&1
  sleep 10
}

echo "  === identical load through each path, one gateway pod killed abruptly ==="
run_path "custom balancer" "http://balancer:8080"
run_path "plain Service"   "http://gateway-direct:8000"
kubectl delete job pathbench --ignore-not-found >/dev/null 2>&1
