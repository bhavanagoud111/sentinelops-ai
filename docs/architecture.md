# Architecture

Canonical blueprint: [`SentinelOps_AI_System_Design.md`](../SentinelOps_AI_System_Design.md).
This page tracks what is **built** versus **planned** and records implementation details
the blueprint leaves open.

## Current state (Phases 0–7)

```
 kind cluster (sentinelops-demo)                 Compose project `sentinelops`
 ┌──────────────────────────────┐   OTLP   ┌──────────────────────────────────────────┐
 │ loadgen → checkout → inventory├────────▶│ OTel Collector → Prometheus · Loki · Tempo│
 │            └─▶ payment, worker│          │                     │ rules              │
 └──────────────▲───────────────┘          │                Alertmanager              │
                │ PATCH (allowlisted        │                     │ bearer webhook     │
                │ runbooks, executor SA)    │                     ▼                    │
                │ get/list (reader SA)      │  ingestion ─▶ Postgres (outbox, audit)   │
                │                           │                 │ relay                  │
                │                           │                 ▼                        │
                │                           │  Redpanda: alerts → correlator → incidents│
                │                           │     → investigator (rules | Claude)       │
                │                           │     → API ◀── dashboard (React, SSE)      │
                │                           │        approval (four-eyes) + policy      │
                └───────────────────────────┼──── executor ──▶ verifier ──▶ resolved   │
                                            └──────────────────────────────────────────┘
```
Production topology (Helm/EKS) is the same control plane as pods in `sentinelops-system`
with RDS/MSK (or in-cluster Kafka) — see ADR-012.

| Component | Status | Notes |
|---|---|---|
| Contracts (`AlertEvent`, `Incident`, state machine) | built | JSON Schemas in `packages/contracts/schemas`, drift-checked in CI |
| Demo services (checkout, inventory, payment, worker, loadgen) | built | FastAPI, OTel SDK, stable HTTP semconv |
| Fault injection (scenarios A–E) | built | token-guarded `/admin/faults`, demo services only |
| Collector → Prometheus/Loki/Tempo | built | |
| Alert rules A–E + pipeline health | built | `infra/observability/prometheus/rules` |
| Grafana service overview | built | provisioned, read-only |
| Ingestion gateway (auth, rate limit, validation, dedup) | built | `services/ingestion` |
| Transactional outbox + relay → Redpanda | built | `packages/storage/src/sentinel_storage/outbox.py`, `services/relay` |
| Correlator (ADR-010) | built | `services/correlator`, replay-safe via `processed_events` |
| PostgreSQL + Alembic, hash-chained append-only audit | built | `packages/storage` |
| Incidents REST API + JWT/RBAC (ADR-011) | built | `apps/api` |
| Investigation agents (LangGraph, ADR-005/008) | built | `services/investigator`; rules provider default, Claude optional |
| Read-only telemetry + Kubernetes clients | built | `packages/telemetry-clients` |
| Runbook catalog (allowlist) | built | `packages/policy` |
| kind cluster, least-privilege RBAC, kube-state-metrics | built | `infra/kind`, `scripts/kind-up.sh` |
| Approvals (four-eyes, expiry, request hash) + policy engine | built | `apps/api/src/sentinel_api/routes/remediation.py`, `packages/policy/src/sentinel_policy/engine.py` (ADR-006) |
| Executor (dry-run + fixed JSON patches) and verifier | built | `services/executor`, `services/verifier` |
| Fault injector (chaos SA) | built | `services/faultinjector` |
| React dashboard + SSE (ADR-009) | built | `apps/dashboard`, served by nginx on :13001 |
| Security test suite | built | `tests/security`: ingestion abuse, authz matrix + forged tokens, adversarial model |
| Platform self-observability | built | consumer lag, platform alert rules, "SentinelOps Platform" Grafana dashboard |
| Evaluation + load tooling | built | `scripts/run_evaluation.sh`, `scripts/summarize_evaluation.py`, `scripts/loadtest.py`; results in `docs/evaluation/` |
| Helm chart (ADR-012) | built | `infra/helm/sentinelops`; `make helm-check`, `make helm-test-install` |
| AWS Terraform (ADR-012) | written, validated, never applied | `infra/terraform`; `make terraform-check`; cost + teardown in its README |
| Release pipeline | written, not yet run | `.github/workflows/release.yml` (tag → images + SBOM/provenance → scan → chart) |

## Signals

| Signal | Source | Prometheus / Loki / Tempo identity |
|---|---|---|
| Request rate, errors, latency | `http.server.request.duration` (OTel FastAPI instrumentation) | `http_server_request_duration_seconds_{bucket,count,sum}` with `service_name`, `http_response_status_code`, `http_route` |
| Business outcome | `checkout_orders` counter | `checkout_orders_total{outcome}` |
| Logs | stdlib logging → OTel LoggingHandler | Loki stream `{service_name="checkout"}`, `trace_id` as structured metadata |
| Traces | OTel SDK | Tempo, `resource.service.name` |

Recording rules (`service:http_requests:rate1m`, `service:http_errors_5xx:ratio1m`,
`service:http_latency_seconds:p95_1m`) are the stable interface for alerts and dashboards;
raw metric names only appear in the recording rules.

## Decisions taken during implementation
- Since Phase 4 the demo workload runs in kind; the Compose demo services remain behind the
  `compose-workload` profile. Pods reach Compose services by name because the kind node joins
  the `sentinelops` Docker network (ADR-007).
- Loki and Tempo store data in the container layer: restart = clean slate for demos.
  Prometheus keeps a named volume (`make destroy` removes it).
- Tempo is pinned to 2.10.x; Tempo 3.x changed its ingest architecture (ADR-003).
- Alertmanager posts to the ingestion gateway with a bearer token read from a Compose secret.

## Open questions
- Multi-service grouping (e.g. checkout 5xx caused by inventory latency) is left to the investigator (ADR-010).

## Known limitations
- Latency quantiles use the OTel SDK's default histogram buckets (…, 1 s, 2.5 s, …), so
  p95 is coarse: a 1.2 s injected delay reads as ≈2.4 s. Add explicit bucket boundaries
  via an SDK `View` if precise latency SLOs are needed.
- `rate()` misses the first increment of a brand-new series (e.g. the first 5xx status
  code seen), slightly delaying the error ratio at fault onset.
