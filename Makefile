.PHONY: help install dev up down logs test lint fmt demo clean

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

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
