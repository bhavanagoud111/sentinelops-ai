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
BIN := $(CURDIR)/.cache/bin
KUBECTL := "$(BIN)/kubectl" --kubeconfig "$(CURDIR)/.cache/kube/admin.kubeconfig"
KIND_VERSION := v0.33.0
KUBECTL_VERSION := v1.37.1
HELM_VERSION := v4.3.0
KUBECONFORM_VERSION := v0.8.0
TERRAFORM_VERSION := 1.16.5
OS := $(shell uname -s | tr A-Z a-z)
ARCH := $(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')

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

# ------------------------------------------------------------------ tools (repo-local)
.PHONY: tools
tools: ## Download pinned kind/kubectl/helm/kubeconform into .cache/bin (no global install)
	@mkdir -p "$(BIN)"
	@[ -x "$(BIN)/kind" ] || curl -sSfLo "$(BIN)/kind" https://kind.sigs.k8s.io/dl/$(KIND_VERSION)/kind-$(OS)-$(ARCH)
	@[ -x "$(BIN)/kubectl" ] || { curl -sSfLo "$(BIN)/kubectl" https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/$(OS)/$(ARCH)/kubectl && \
	  echo "$$(curl -sSfL https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/$(OS)/$(ARCH)/kubectl.sha256)  $(BIN)/kubectl" | shasum -a 256 -c >/dev/null; }
	@[ -x "$(BIN)/helm" ] || { curl -sSfL https://get.helm.sh/helm-$(HELM_VERSION)-$(OS)-$(ARCH).tar.gz | tar -xz -C "$(BIN)" --strip-components=1 $(OS)-$(ARCH)/helm; }
	@[ -x "$(BIN)/kubeconform" ] || { curl -sSfL https://github.com/yannh/kubeconform/releases/download/$(KUBECONFORM_VERSION)/kubeconform-$(OS)-$(ARCH).tar.gz | tar -xz -C "$(BIN)" kubeconform; }
	@chmod +x "$(BIN)"/kind "$(BIN)"/kubectl "$(BIN)"/helm "$(BIN)"/kubeconform
	@"$(BIN)/kind" version && "$(BIN)/kubectl" version --client | head -1 && "$(BIN)/helm" version --short

.PHONY: helm-check
helm-check: tools ## Lint + render the Helm chart (default and all-features values)
	@"$(BIN)/helm" lint --strict infra/helm/sentinelops
	@"$(BIN)/helm" template ci infra/helm/sentinelops >/dev/null
	@for v in "" "-f infra/helm/sentinelops/ci/all-features-values.yaml"; do \
	  "$(BIN)/helm" template ci infra/helm/sentinelops $$v | "$(BIN)/kubeconform" -strict -summary - || exit 1; \
	done

.PHONY: helm-test-install helm-test-uninstall
helm-test-install: tools ## Install the chart into kind (stops the Compose pipeline while it runs)
	@scripts/helm-test.sh install

helm-test-uninstall: ## Remove the kind Helm release and restart the Compose pipeline
	@scripts/helm-test.sh uninstall

.PHONY: terraform-check
TF := TF_DATA_DIR="$(CURDIR)/.cache/terraform/demo" TF_PLUGIN_CACHE_DIR="$(CURDIR)/.cache/terraform/plugins" \
	"$(BIN)/terraform" -chdir=infra/terraform/envs/demo
terraform-check: ## fmt + validate the AWS Terraform (no credentials, no backend, creates nothing)
	@mkdir -p "$(BIN)" .cache/terraform/plugins
	@[ -x "$(BIN)/terraform" ] || { curl -sSfLo .cache/terraform/tf.zip \
	  https://releases.hashicorp.com/terraform/$(TERRAFORM_VERSION)/terraform_$(TERRAFORM_VERSION)_$(OS)_$(ARCH).zip && \
	  unzip -oq .cache/terraform/tf.zip terraform -d "$(BIN)" && rm .cache/terraform/tf.zip; }
	@"$(BIN)/terraform" fmt -check -recursive infra/terraform
	@$(TF) init -backend=false -input=false >/dev/null
	@$(TF) validate

.PHONY: manifests-check
manifests-check: tools ## Offline schema validation of the kind manifests
	@"$(BIN)/kubeconform" -strict -summary infra/kind/rbac.yaml infra/kind/kube-state-metrics.yaml infra/kind/workloads.yaml

# ------------------------------------------------------------------ stack
.PHONY: up down destroy ps logs build config secrets migrate network kind-up kind-down compose-up kubectl
secrets: ## Generate per-machine random secrets into .cache/secrets (idempotent)
	@mkdir -p .cache/secrets
	@for s in ingest_token jwt_secret faultinjector_token; do \
	  [ -s .cache/secrets/$$s ] || { openssl rand -hex 32 > .cache/secrets/$$s && echo "generated .cache/secrets/$$s"; }; \
	  chmod 644 .cache/secrets/$$s; \
	done

config: secrets ## Validate compose file
	$(COMPOSE) config --quiet && echo "compose config OK"

build: secrets ## Build images
	$(COMPOSE) build

network:
	@$(DOCKER) network inspect sentinelops >/dev/null 2>&1 || $(DOCKER) network create sentinelops >/dev/null

kind-up: network tools ## Create/refresh the kind cluster with the demo workload
	@scripts/kind-up.sh

kind-down: ## Delete the kind cluster
	-"$(BIN)/kind" delete cluster --name sentinelops

compose-up: secrets network ## Start the Compose control plane + observability
	$(COMPOSE) up -d --build --wait
	@curl -s -X POST http://127.0.0.1:$${PROMETHEUS_PORT:-19090}/-/reload >/dev/null || true

kubectl: ## Run kubectl against the demo cluster: make kubectl ARGS="get pods"
	@$(KUBECTL) -n sentinelops-demo $(ARGS)

up: secrets network kind-up compose-up ## Start everything (kind workload + Compose control plane)
	@echo ""
	@echo "  Grafana       http://127.0.0.1:$${GRAFANA_PORT:-13000}"
	@echo "  Prometheus    http://127.0.0.1:$${PROMETHEUS_PORT:-19090}"
	@echo "  Alertmanager  http://127.0.0.1:$${ALERTMANAGER_PORT:-19093}"
	@echo "  Dashboard     http://127.0.0.1:$${DASHBOARD_PORT:-13001}"
	@echo "  API docs      http://127.0.0.1:$${API_PORT:-18000}/docs"
	@echo "  Checkout API  http://127.0.0.1:$${CHECKOUT_PORT:-18081}/docs"

down: ## Stop Compose and pause the kind node (keeps data)
	$(COMPOSE) down
	-$(DOCKER) stop sentinelops-control-plane >/dev/null

destroy: ## Delete Compose volumes and the kind cluster (SentinelOps only)
	$(COMPOSE) down -v --remove-orphans
	-"$(BIN)/kind" delete cluster --name sentinelops

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

ps: ## Show stack status
	$(COMPOSE) ps

logs: ## Tail logs (SERVICE=checkout to filter)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

# ------------------------------------------------------------------ demo
.PHONY: inject-fault inject-latency clear-fault fault-status reset-demo verify-telemetry scenario
scenario: ## Inject a demo scenario: make scenario S=A   (A-E, see docs/runbooks.md)
	@scripts/inject-fault.sh $(S)

inject-fault: ## Scenario A: checkout 5xx via a bad config rollout
	@scripts/inject-fault.sh A

inject-latency: ## Scenario B: inventory latency via a feature-flag rollout
	@scripts/inject-fault.sh B

fault-status: ## Show demo workload revisions and runtime faults
	@scripts/inject-fault.sh status

reset-demo: ## Restore the demo workload baseline and clear runtime faults
	@scripts/inject-fault.sh reset

verify-telemetry: ## Phase 1 exit gate: metric + log + trace + alert for scenario A
	$(UV) python scripts/verify_telemetry.py
