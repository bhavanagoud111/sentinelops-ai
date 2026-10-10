# SentinelOps AI — developer entrypoints. Run `make help`.
SHELL := /bin/bash
.DEFAULT_GOAL := help

-include .env
export

# Keep every tool cache inside the repo; never write to the user's home.
export UV_CACHE_DIR := $(CURDIR)/.cache/uv
export UV_PYTHON_INSTALL_DIR := $(CURDIR)/.cache/python
export UV_PYTHON_PREFERENCE := only-managed
export UV_PYTHON_INSTALL_BIN := 0

# Docker Desktop's CLI is not always on PATH on macOS. Its credential helper lives in the
# same directory, so it is appended to PATH for child processes too.
DOCKER_DESKTOP_BIN := /Applications/Docker.app/Contents/Resources/bin
DOCKER ?= $(or $(shell command -v docker 2>/dev/null),$(wildcard $(DOCKER_DESKTOP_BIN)/docker),docker)
export PATH := $(PATH):$(DOCKER_DESKTOP_BIN)

UV := uv run --frozen
COMPOSE := $(DOCKER) compose -f infra/compose/docker-compose.yml

.PHONY: help
help: ## Show targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# ------------------------------------------------------------------ toolchain
.PHONY: bootstrap sync
bootstrap: ## Check prerequisites, install Python 3.12 + deps into the repo
	@scripts/bootstrap.sh

sync: ## Install/refresh Python deps from uv.lock
	uv python install 3.12
	uv sync --frozen

# ------------------------------------------------------------------ quality
.PHONY: fmt lint typecheck test test-integration schemas check
fmt: ## Format code
	$(UV) ruff format .
	$(UV) ruff check . --fix

lint: ## Lint + format check
	$(UV) ruff check .
	$(UV) ruff format --check .

typecheck: ## mypy (strict)
	$(UV) mypy packages demo services apps tests scripts

test: ## Unit tests
	$(UV) pytest

test-integration: ## Integration tests against the running stack
	$(UV) pytest -m integration -o addopts="-ra --strict-markers"

schemas: ## Regenerate JSON Schemas for contracts
	$(UV) python scripts/export_schemas.py

check: lint typecheck test ## Everything CI runs (minus Docker)
	$(UV) python scripts/export_schemas.py --check

# ------------------------------------------------------------------ stack
.PHONY: up down destroy ps logs build config secrets migrate
secrets: ## Generate per-machine random secrets into .cache/secrets (idempotent)
	@mkdir -p .cache/secrets
	@for s in ingest_token jwt_secret; do \
	  [ -s .cache/secrets/$$s ] || { openssl rand -hex 32 > .cache/secrets/$$s && echo "generated .cache/secrets/$$s"; }; \
	  chmod 644 .cache/secrets/$$s; \
	done

config: secrets ## Validate compose file
	$(COMPOSE) config --quiet && echo "compose config OK"

build: secrets ## Build images
	$(COMPOSE) build

up: secrets ## Start the local stack
	$(COMPOSE) up -d --build --wait
	@echo ""
	@echo "  Grafana       http://127.0.0.1:$${GRAFANA_PORT:-13000}"
	@echo "  Prometheus    http://127.0.0.1:$${PROMETHEUS_PORT:-19090}"
	@echo "  Alertmanager  http://127.0.0.1:$${ALERTMANAGER_PORT:-19093}"
	@echo "  API docs      http://127.0.0.1:$${API_PORT:-18000}/docs"
	@echo "  Checkout API  http://127.0.0.1:$${CHECKOUT_PORT:-18081}/docs"

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

destroy: ## Stop the stack and delete its volumes (SentinelOps only)
	$(COMPOSE) down -v --remove-orphans

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

ps: ## Show stack status
	$(COMPOSE) ps

logs: ## Tail logs (SERVICE=checkout to filter)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

# ------------------------------------------------------------------ demo
.PHONY: inject-fault inject-latency clear-fault fault-status reset-demo verify-telemetry
inject-fault: ## Scenario A: checkout 5xx via bad config
	@scripts/inject-fault.sh checkout-5xx $(P)

inject-latency: ## Scenario B: inventory dependency delay
	@scripts/inject-fault.sh inventory-latency $(DELAY)

clear-fault: ## Clear all demo faults
	@scripts/inject-fault.sh clear

fault-status: ## Show active demo faults
	@scripts/inject-fault.sh status

reset-demo: ## Clear faults and restart demo services to a clean state
	-@scripts/inject-fault.sh clear
	$(COMPOSE) restart inventory checkout loadgen

verify-telemetry: ## Phase 1 exit gate: metric + log + trace + alert for scenario A
	$(UV) python scripts/verify_telemetry.py
