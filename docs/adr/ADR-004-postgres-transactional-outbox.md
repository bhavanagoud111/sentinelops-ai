# ADR-004: PostgreSQL as system of record with transactional outbox

**Status:** Accepted · 2026-10-09 (implemented in Phase 2)

## Context
Ingestion must durably accept an alert *and* publish it to the broker. Writing to the DB
and the broker separately risks lost or phantom events on crash (dual-write problem).

## Decision
PostgreSQL 16+ is the source of truth for incidents, evidence, approvals, executions and
audit. The gateway writes the normalized `AlertEvent` **and** an `outbox_events` row in the
same transaction. A separate publisher reads unpublished outbox rows, publishes to the
broker keyed by fingerprint, and marks them published. Consumers dedupe on `event_id`
(unique constraint). Contracts carry `schema_version`; breaking changes need a new version
and a new ADR. Migrations via Alembic.

## Alternatives
- *Publish then write*: lost DB state if the write fails after publish.
- *CDC (Debezium)*: robust, but another heavy component for a local demo.
- *Broker as source of truth*: harder to query, approve and audit.

## Consequences
+ No dual-write window; replay is safe.
− Publish latency adds the outbox poll interval (target < 1 s).
− Outbox table needs pruning.
