# SentinelOps AI — System Design & Architecture Specification

**Version:** 1.0 | **Status:** Proposed architecture | **Date:** 2026-10-09

**Purpose:** Canonical implementation blueprint for an AI-assisted SRE incident detection, diagnosis, approval, and recovery platform.

**Core design invariant:** An LLM can analyze and recommend, but cannot independently execute arbitrary infrastructure operations.

## 1. Executive Summary

SentinelOps AI is a portfolio-grade, production-inspired AI SRE platform. It ingests telemetry from an instrumented microservice environment, detects incidents using deterministic signals, correlates metrics/logs/traces/deployments, uses bounded AI agents to investigate root causes, proposes remediations, and executes only explicitly approved allowlisted actions. It is NOT intended for unsupervised management of real production infrastructure.

## 2. Goals, Non-goals and Success Criteria

Goals: reproducible local demo; credible distributed systems architecture; traceable incident diagnosis; secure human-in-the-loop remediation; measurable operational performance; polished UI and developer experience. Non-goals: general-purpose cloud management, arbitrary shell access for LLMs, guaranteed root cause certainty, full enterprise multi-tenancy in v1, or automatic production deployment. Acceptance: five seeded incidents; at least four correctly identified with evidence in a fixed evaluation suite (target, not claimed result); zero unauthorized actions in adversarial tests; complete incident audit trail; successful clean-machine bootstrap.

## 3. Primary Personas and User Journeys

SRE: receives incident, inspects correlated telemetry and agent reasoning, approves or rejects remediation, checks outcome. Platform engineer: configures telemetry integrations and service ownership, reviews incident trends. Recruiter/demo viewer: launches reproducible fault scenario and watches incident-to-resolution lifecycle. Journey: inject fault -> signal breach -> deduplicate and correlate -> gather evidence -> hypothesize -> verify -> propose runbook -> human approval -> policy check -> execute -> verify health -> close or escalate.

## 4. High-Level Architecture

Instrumented microservices (OpenTelemetry SDK/Collector, Prometheus exporters) -> OTel Collector -> Prometheus (metrics), Loki (logs), Tempo (traces). Prometheus Alertmanager webhooks and optional Kubernetes events -> FastAPI ingestion gateway -> Kafka topics -> incident correlator -> PostgreSQL incident store -> LangGraph investigation orchestrator -> read-only telemetry adapters -> evidence store -> recommendation and policy engine -> approval API/UI -> bounded Kubernetes action executor -> verification worker -> audit/event log. React dashboard uses FastAPI REST and Server-Sent Events. Grafana provides independent telemetry exploration. Redis is optional for caching/rate limits, not source of truth.

## 5. Component Responsibilities

Demo microservices: storefront API, checkout API, inventory API, payment simulator, PostgreSQL/Redis where needed; OpenTelemetry Demo can be integrated as a larger optional workload. Collector: standardized traces/logs/metrics forwarding. Prometheus: metrics and alert rules. Loki: searchable logs. Tempo: distributed traces. Alertmanager: alert grouping and delivery. Kafka: decoupled durable event transport. Gateway: webhook auth, schema validation, idempotency and publishing. Correlator: incident fingerprinting and time-window grouping. PostgreSQL: incidents, evidence pointers, hypotheses, approvals, actions and audit. Agent service: LangGraph state machine with typed tools. Executor: constrained Kubernetes operations. Frontend: incident timeline, topology, evidence and approvals.

## 6. Technology Choices and Tradeoffs

Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy/Alembic; LangGraph for explicit bounded agent state; Kafka-compatible Redpanda locally (Apache Kafka optional for parity); PostgreSQL 16+; React/TypeScript/Vite/TanStack Query; OpenTelemetry Collector, Prometheus, Alertmanager, Loki, Tempo, Grafana; Docker Compose for control plane; kind or k3d for Kubernetes workload; pytest, Playwright, Ruff, mypy, GitHub Actions. AWS phase: EKS, MSK or managed compatible broker, RDS PostgreSQL, ECR, IAM/IRSA, Secrets Manager, CloudWatch optional. Keep the local demo vendor-neutral and do not require paid LLM APIs; support a mock deterministic model and configurable LLM adapter.

## 7. Data Flow and Event Contracts

Alert webhook POST /api/v1/ingest/alertmanager -> verify shared secret or signed ingress proxy -> normalize AlertEvent{event_id, source, fingerprint, service, environment, severity, starts_at, labels, annotations, received_at, schema_version} -> transactional outbox row + database commit -> outbox publisher -> sentinel.alerts.v1 (key=fingerprint). Correlator consumes and creates/updates Incident; publishes sentinel.incidents.v1. Investigation orchestrator records evidence/hypotheses, emits sentinel.investigations.v1. Approval emits sentinel.approvals.v1. Executor emits sentinel.actions.v1. All consumers must be idempotent; use event_id uniqueness and retry/dead-letter topics. Do not promise exactly-once end-to-end; use at-least-once with deduplication.

## 8. Incident State Machine

OPEN -> TRIAGING -> INVESTIGATING -> AWAITING_APPROVAL -> REMEDIATING -> VERIFYING -> RESOLVED. Alternative transitions: INVESTIGATING -> NEEDS_HUMAN; AWAITING_APPROVAL -> REJECTED; REMEDIATING/VERIFYING -> FAILED or NEEDS_HUMAN; resolved incidents may REOPEN on recurrence. Persist every transition with actor, reason, timestamp, correlation ID, previous and next states. Optimistic locking prevents double approvals/actions. Timeout, cancellation and max-step limits are mandatory.

## 9. Multi-Agent Design

LangGraph nodes: (1) Triage Agent classifies impact and extracts service context; (2) Evidence Collector uses typed read-only tools for PromQL, Loki LogQL, Tempo trace search, Kubernetes get/list and deployment history; (3) Root Cause Agent creates ranked hypotheses with supporting and contradicting evidence; (4) Critic/Verifier checks whether hypotheses match telemetry and avoids fabricated claims; (5) Remediation Planner maps verified hypotheses to allowlisted runbooks and estimates blast radius; (6) Reporter generates concise incident summary. Orchestrator controls transitions and budgets; no free-form autonomous agent-to-agent tool execution. Tool results contain query, time range, source, retrieval timestamp, bounded output, and immutable evidence ID. Each conclusion must cite evidence IDs. If evidence is insufficient, abstain/escalate.

## 10. Tool Interfaces and LLM Contracts

Tools: query_metrics(promql, start, end, step); query_logs(logql, start, end, limit); get_trace(trace_id); list_pods(namespace, label_selector); get_pod_events(namespace, pod); get_deployment(namespace, name); get_recent_rollouts(namespace, deployment); fetch_runbook(runbook_id). Read tools validate parameters, restrict namespace/label scopes, enforce result-size/time budgets and redact secrets. Structured agent output: hypothesis_id, cause_category, explanation, confidence (calibrated later), evidence_ids[], counterevidence_ids[], recommended_runbook_id?, uncertainty, next_checks[]. Treat logs and tool output as untrusted data; reject prompt-injection instructions. Model provider interface supports mocked outputs for CI and configurable hosted/local models.

## 11. Remediation Safety Model

Default is recommend-only. In demo mode, approved runbooks may: restart a specific labeled demo deployment via rollout restart; rollback a deployment to a previously verified revision; scale a demo deployment within predefined min/max; disable a demo feature flag. Never permit arbitrary kubectl, shell, SQL or unrestricted model-generated commands. Two-stage checks: approval with authenticated human identity, expiry and incident/action binding; policy engine checks namespace, service, runbook, parameters, RBAC, current deployment version, concurrency and cooldown. Executor uses dedicated Kubernetes service account with least privilege; dry-run where supported; records before/after state, operator, command template, result, and verification. If checks fail, stop and escalate. Disable write access entirely outside sandbox until separately reviewed.

## 12. Database Model

Tables: services(id,name,namespace,owner,criticality); incidents(id,fingerprint,status,severity,service_id,first_seen,last_seen,summary,version); alert_events(id,event_id,incident_id,payload,received_at); evidence(id,incident_id,source,query_ref,time_range,content_hash,summary,created_at); hypotheses(id,incident_id,rank,description,confidence,evidence_refs,created_at); runbooks(id,action_type,allowed_scopes,parameter_schema,risk_level,version); approvals(id,incident_id,runbook_id,requested_by,approved_by,status,expires_at,request_hash); executions(id,incident_id,approval_id,runbook_id,status,idempotency_key,before_state,after_state,started_at,finished_at); incident_transitions(id,incident_id,from_status,to_status,actor,reason,at); audit_events(id,actor,action,resource_id,metadata,at); outbox_events(id,topic,key,payload,published_at). JSONB for bounded metadata; indexed fingerprint/status/service/time; migrations via Alembic. Avoid storing raw secrets and excessive raw log payloads.

## 13. API Design

POST /api/v1/ingest/alertmanager (machine authenticated); GET /api/v1/incidents?status=&service=&cursor=; GET /api/v1/incidents/{id}; GET /api/v1/incidents/{id}/timeline; GET /api/v1/incidents/{id}/evidence; POST /api/v1/incidents/{id}/investigate; POST /api/v1/incidents/{id}/approvals (request); POST /api/v1/approvals/{id}/decision {decision,reason}; POST /api/v1/incidents/{id}/actions/{runbook_id}/execute (requires valid approval); GET /api/v1/services; GET /api/v1/events/stream (SSE). Document request/response/error schemas in OpenAPI. JWT/OIDC for users, scoped service tokens for ingest, RBAC roles viewer/operator/approver/admin. Mutating endpoints require idempotency keys and audit.

## 14. Frontend UX

Pages: overview with incident counts, status and service health; incidents list with filters; incident detail with severity, affected service, impact timeline, correlated metrics/logs/traces and hypothesis evidence; agent activity timeline with tool calls and uncertainty; remediation panel with runbook diff, risk and approval decision; service topology; experiments page for triggering safe sandbox faults; settings/integrations. Use Grafana deep links to metrics, logs and traces. Show real persisted state, not hard-coded demo values. Accessible status colors, loading/error/empty states and mobile-friendly layouts.

## 15. Fault Injection and Demo Scenarios

Build five repeatable fault cases: (A) checkout 5xx spike due to bad configuration; (B) inventory latency due to artificial dependency delay; (C) payment simulator timeout and cascading retries; (D) Kubernetes CrashLoopBackOff after faulty image/config rollout; (E) memory/CPU saturation in a demo worker. For each define injection command, expected alert, relevant metrics/logs/traces, root cause evidence, expected agent response, permitted remediation or escalation, and cleanup. All chaos experiments scoped to demo namespaces; never target real production.

## 16. Reliability, Security and Observability

Gateway rate limiting, schema validation, timeouts, retry with exponential backoff, DLQs, consumer lag monitoring, circuit breakers for telemetry queries, bounded LLM token/time budgets, concurrency limits and graceful degradation. Instrument SentinelOps itself with OTel spans, Prometheus metrics, structured JSON logs, correlation IDs and dashboards. Secrets via env for local development and secret manager in cloud; no secrets in Git. TLS in hosted deployment; secure webhook authentication; OIDC and least-privilege RBAC; audit tamper detection via chained hashes optional. Supply-chain scanning, dependency pinning, image scanning and threat model. Consider sensitive log redaction, retention, data minimization and model-provider data boundaries.

## 17. Evaluation and Benchmarks

Create fixed incident dataset with ground-truth fault causes and expected evidence IDs. Metrics: alert-to-incident latency p50/p95; detection precision/recall against injected events; root-cause top-1/top-3 accuracy; evidence citation correctness; false remediation rate; unauthorized action attempts blocked; time-to-recommendation; token/cost per incident; mean time to verified recovery; Kafka throughput/lag and API p95. Compare deterministic rules-only baseline vs AI investigation. Run each scenario multiple times; publish methodology and raw results. Never claim improvements before measuring them.

## 18. Deployment Topology

Local phase: Docker Compose for gateway, orchestrator, worker, Postgres, Redpanda, Collector, Prometheus, Alertmanager, Loki, Tempo, Grafana, frontend; kind Kubernetes cluster for demo microservices and scoped executor access. Prefer deploying control plane into kind later if host-to-cluster access becomes cumbersome. Kubernetes phase: Helm charts for application services, NetworkPolicies, dedicated service accounts, resource limits, probes, HPA where appropriate. Cloud optional: Terraform modules for VPC/EKS/RDS/ECR, managed Kafka alternative, ingress TLS and observability; document monthly cost and teardown. Do not require AWS for core demo.

## 19. Repository Layout

sentinelops-ai/
  README.md
  docs/{architecture.md,adr/,api.md,threat-model.md,runbooks.md,evaluation.md}
  apps/{dashboard/,api/}
  services/{ingestion/,correlator/,investigator/,executor/,verifier/}
  packages/{contracts/,telemetry-clients/,policy/,common/}
  demo/{microservices/,faults/,datasets/}
  infra/{compose/,kind/,helm/,terraform/,observability/}
  tests/{unit/,integration/,e2e/,security/,evaluation/}
  scripts/{bootstrap.sh,seed.sh,inject-fault.sh,reset-demo.sh}
  .github/workflows/{ci.yml,security.yml}
  CONTRIBUTING.md
  .env.example
  Makefile
Use a monorepo; services may initially be modular Python packages and split into processes only when justified. Pin dependencies and document tool versions.

## 20. Implementation Roadmap

Phase 0 (foundation): ADRs, repo skeleton, local bootstrap, architecture contracts, CI, threat model. Phase 1 (telemetry): demo services, OTel instrumentation, Prometheus/Loki/Tempo/Grafana, one alert and trace. Phase 2 (incident pipeline): gateway, broker, outbox, correlator, Postgres, state transitions, REST. Phase 3 (AI): typed read-only telemetry tools, bounded LangGraph workflow, evidence citations, deterministic mock mode, evaluation fixtures. Phase 4 (remediation): allowlisted runbooks, approvals, policy checks, scoped Kubernetes executor, verification, audit. Phase 5 (product): React UI, SSE, Grafana deep links, fault injector and five demos. Phase 6 (hardening): security tests, load tests, observability of platform, reproducible benchmarks and polished README. Phase 7 (optional): AWS deployment and CI release pipeline. Each phase must be independently demoable and merged only after tests pass.

## 21. Milestone Exit Gates

P0: new clone boots infrastructure and CI green. P1: one fault produces visible metric, log, trace and alert. P2: alert generates one durable deduplicated incident; restart/replay causes no duplicates. P3: agent produces evidence-backed hypotheses with abstention on missing evidence; mock mode passes fixtures. P4: rejection prevents execution, expired approval fails, cross-namespace action fails, approved demo action succeeds and is audited. P5: full fault-to-resolution UI demo with no mocked incident state. P6: five reproducible scenarios, published evaluation and security test results. P7: cloud teardown and cost budget documented.

## 22. Implementation Principles

This document is the architecture baseline, not a mandate to build everything at once. Work one phase and one vertical slice at a time, starting from docs/architecture.md and CONTRIBUTING.md. Preserve existing APIs and migrations or propose ADR. Add tests, typing, structured logging, docs and minimal examples with every change. Never fabricate benchmarks or success claims. Never commit secrets, cloud credentials or generated production data. Never enable unsafe autonomous remediation or broad Kubernetes permissions. Prefer deterministic, local, low-cost integrations. Format, lint, type-check and test every change. Destructive operations, infrastructure spending, schema-breaking changes and security-policy changes require explicit review.

## 23. First Build Slice

Deliver a complete, narrow vertical slice: two demo services (checkout and inventory); OTel traces/metrics; Prometheus alert on injected checkout 5xx; Alertmanager -> FastAPI gateway -> Redpanda -> correlator -> PostgreSQL incident; GET incidents endpoint; simple incident dashboard. No LLM, Kubernetes writes or complex multi-agent workflow yet. Definition of done: `make up`, `make inject-fault`, incident appears in API/UI, `make reset-demo`, and integration test validates deduplication. This proves the foundation before agent complexity.

## 24. Architectural Decisions to Record

ADR-001 monorepo and modular services; ADR-002 local Redpanda vs Kafka; ADR-003 OTel/Prometheus/Loki/Tempo observability; ADR-004 PostgreSQL plus transactional outbox; ADR-005 LangGraph with bounded typed tools; ADR-006 manual approval and allowlisted Kubernetes runbooks; ADR-007 Docker Compose plus kind; ADR-008 mock LLM provider for deterministic CI; ADR-009 SSE before WebSockets. Each ADR should include context, decision, alternatives and consequences.

## 25. References and Integrations

OpenTelemetry Demo: https://github.com/open-telemetry/opentelemetry-demo ; OpenTelemetry: https://opentelemetry.io/docs/ ; Prometheus: https://prometheus.io/docs/ ; Grafana Loki: https://grafana.com/docs/loki/latest/ ; Grafana Tempo: https://grafana.com/docs/tempo/latest/ ; LangGraph: https://docs.langchain.com/oss/python/langgraph/overview ; Kafka: https://kafka.apache.org/documentation/ ; Kubernetes: https://kubernetes.io/docs/ ; FastAPI: https://fastapi.tiangolo.com/ ; Temporal (future alternative): https://temporal.io/ . Verify current compatibility and licenses during implementation.
