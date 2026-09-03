.PHONY: help install dev up down logs test lint fmt demo clean \
        kind-up kind-down k8s-apply k8s-secret k8s-keda k8s-status k8s-scale-demo

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install dev dependencies into the active venv
	pip install -r requirements-dev.txt

dev:  ## Run the gateway on the host (needs a local redis on :6379)
	REDIS_URL=redis://localhost:6379/0 uvicorn app.main:app --reload --port 8000

up:  ## Start the stack
	docker compose up --build -d

down:  ## Stop the stack and remove volumes
	docker compose down -v

logs:  ## Follow gateway logs
	docker compose logs -f gateway

test:  ## Run the test suite (mock mode, no secrets needed)
	pytest -q

lint:  ## Lint
	ruff check app tests

fmt:  ## Format
	ruff format app tests

demo:  ## The Ring 0 demo: identical prompts, second one cached
	@./scripts/demo.sh

kind-up:  ## Create the kind cluster and load images (stop compose first)
	docker compose down 2>/dev/null || true
	kind create cluster --config kind-config.yaml
	docker compose build
	kind load docker-image ai-router-gateway:latest ai-router-balancer:latest --name ai-router

kind-down:  ## Delete the kind cluster
	kind delete cluster --name ai-router

k8s-secret:  ## Create router-secrets from .env -- only the secret keys
	@grep -E '^(ANTHROPIC_API_KEY|GATEWAY_API_KEYS)=' .env > .secret.env 2>/dev/null || true
	@kubectl create secret generic router-secrets --from-env-file=.secret.env \
	  --dry-run=client -o yaml | kubectl apply -f -
	@rm -f .secret.env

k8s-apply: k8s-secret  ## Apply manifests to the current cluster
	kubectl apply -k k8s/overlays/local
	kubectl rollout status deploy/redis --timeout=120s
	kubectl rollout status deploy/gateway --timeout=180s
	kubectl rollout status deploy/balancer --timeout=180s

k8s-keda:  ## Install KEDA and the ScaledObject
	helm repo add kedacore https://kedacore.github.io/charts
	helm repo update
	helm upgrade --install keda kedacore/keda -n keda --create-namespace --wait
	kubectl apply -f k8s/keda/scaledobject.yaml

k8s-status:  ## Pods, scaling target, and replica discovery
	@kubectl get pods
	@kubectl get scaledobject 2>/dev/null || true
	@curl -s localhost:8000/stats | python3 -m json.tool | head -20

k8s-scale-demo:  ## Drive load and watch KEDA scale the gateway
	@echo "watch in another shell: kubectl get pods -w"
	python load/driver.py --duration 180 --concurrency 60

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
