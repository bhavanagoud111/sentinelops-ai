# ADR-003: OpenTelemetry + Prometheus + Loki + Tempo + Grafana

**Status:** Accepted · 2026-10-09

## Context
Agents in Phase 3 need to query metrics, logs and traces through stable, open APIs
(PromQL, LogQL, TraceQL). The stack must be vendor-neutral, free, and runnable locally.

## Decision
- Services emit all three signals with the **OpenTelemetry SDK** over OTLP/HTTP to a
  single **OTel Collector**, which fans out to **Prometheus** (scrape of the collector's
  exporter), **Loki** (native OTLP endpoint) and **Tempo** (OTLP gRPC).
- HTTP metrics use **stable semantic conventions** (`OTEL_SEMCONV_STABILITY_OPT_IN=http`).
- Alerts and dashboards depend on **recording rules**, not raw metric names.
- **Grafana** is provisioned read-only with datasources cross-linked on `trace_id`.
- Pinned versions: collector-contrib 0.162.0, Prometheus 3.15.0, Alertmanager 0.34.1,
  Loki 3.7.8, Tempo 2.10.8, Grafana 12.4.12. Tempo stays on 2.x because 3.x changed its
  ingest architecture; revisit when there is a reason.

## Alternatives
- *Prometheus client libraries + promtail*: simpler metrics, but three instrumentation
  styles instead of one.
- *Jaeger* for traces: mature, but Tempo integrates with Loki/Grafana and TraceQL.
- *Hosted (Grafana Cloud, Datadog)*: violates the zero-cost, local-first goal.

## Consequences
+ One instrumentation API; collector is the single place to add processing/redaction.
+ Phase 3 tools get standard query languages.
− Collector metric-name translation is a coupling point (mitigated by recording rules).
− Six observability containers: ~500 MB RAM measured at demo load (2026-10-09).
