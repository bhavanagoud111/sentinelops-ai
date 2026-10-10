# Architecture

Canonical blueprint: [`SentinelOps_AI_System_Design.md`](../SentinelOps_AI_System_Design.md).
This page tracks what is **built** versus **planned** and records implementation details
the blueprint leaves open.

## Current state (Phase 0 + Phase 1)

```
            ┌──────────┐  HTTP   ┌──────────┐  HTTP   ┌───────────┐
            │ loadgen  │───────▶│ checkout │───────▶│ inventory │
            └──────────┘         └────┬─────┘         └─────┬─────┘
                                      │ OTLP/HTTP (traces, metrics, logs)
                                      ▼                     ▼
                               ┌─────────────────────────────────┐
                               │       OpenTelemetry Collector    │
                               └──────┬──────────┬──────────┬────┘
                                traces│   metrics│      logs│
                                      ▼          ▼          ▼
                                   Tempo    Prometheus     Loki
                                               │ rules
                                               ▼
                                         Alertmanager ──▶ (Phase 2: ingestion gateway)
                                               
                        Grafana ◀── Prometheus + Loki + Tempo (linked by trace_id)
```

| Component | Status | Notes |
|---|---|---|
| Contracts (`AlertEvent`, `Incident`, state machine) | built | JSON Schemas in `packages/contracts/schemas`, drift-checked in CI |
| Demo services (checkout, inventory, loadgen) | built | FastAPI, OTel SDK, stable HTTP semconv |
| Fault injection (scenarios A, B) | built | token-guarded `/admin/faults`, demo services only |
| Collector → Prometheus/Loki/Tempo | built | |
| Alert rules A, B + pipeline health | built | `infra/observability/prometheus/rules` |
| Grafana service overview | built | provisioned, read-only |
| Ingestion gateway, outbox, Redpanda, correlator, Postgres | Phase 2 | |
| Investigation agents | Phase 3 | |
| Approvals, policy, executor (kind) | Phase 4 | |
| React dashboard, SSE | Phase 5 | |

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
- Phase 1 runs the demo workload in Docker Compose, not kind. kind arrives in Phase 4 when
  the executor needs a real Kubernetes API (ADR-007).
- Loki and Tempo store data in the container layer: restart = clean slate for demos.
  Prometheus keeps a named volume (`make destroy` removes it).
- Tempo is pinned to 2.10.x; Tempo 3.x changed its ingest architecture (ADR-003).
- Alertmanager routes to a `null` receiver in Phase 1. Phase 2 adds the gateway webhook.

## Open questions (to resolve in Phase 2)
- Correlation window and re-open policy for a recurring fingerprint.
- Multi-service grouping (e.g. checkout 5xx caused by inventory latency).
- `evidence` table needs an explicit immutable `evidence_id` (spec §9 vs §12).

## Known limitations
- Latency quantiles use the OTel SDK's default histogram buckets (…, 1 s, 2.5 s, …), so
  p95 is coarse: a 1.2 s injected delay reads as ≈2.4 s. Add explicit bucket boundaries
  via an SDK `View` if precise latency SLOs are needed.
- `rate()` misses the first increment of a brand-new series (e.g. the first 5xx status
  code seen), slightly delaying the error ratio at fault onset.
