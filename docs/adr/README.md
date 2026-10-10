# Architecture Decision Records

Format: Context · Decision · Alternatives · Consequences. Status is one of
Proposed, Accepted, Superseded by ADR-NNN.

| ADR | Title | Status |
|---|---|---|
| [001](ADR-001-monorepo-modular-services.md) | Monorepo with modular Python packages | Accepted |
| [002](ADR-002-redpanda-local-broker.md) | Redpanda locally, Kafka API as the contract | Accepted |
| [003](ADR-003-observability-stack.md) | OpenTelemetry + Prometheus + Loki + Tempo + Grafana | Accepted |
| [004](ADR-004-postgres-transactional-outbox.md) | PostgreSQL as system of record with transactional outbox | Accepted |
| [007](ADR-007-compose-then-kind.md) | Docker Compose first, kind for Kubernetes workloads in Phase 4 | Accepted |
| [010](ADR-010-correlation-policy.md) | Incident correlation key and reopen policy | Accepted |
| [011](ADR-011-authn-authz.md) | Authentication and authorization | Accepted |

Planned: 005 LangGraph bounded tools, 006 approval + allowlisted runbooks,
008 mock LLM provider, 009 SSE before WebSockets.
