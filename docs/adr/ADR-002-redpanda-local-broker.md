# ADR-002: Redpanda locally, Kafka API as the contract

**Status:** Accepted · 2026-10-09 (implemented in Phase 2)

## Context
The pipeline needs a durable, replayable event log between ingestion, correlation,
investigation, approval and execution (spec §7). Local demos must start fast on a laptop.

## Decision
Code targets the **Kafka protocol** only. Locally we run a single-node **Redpanda**
container (one binary, no ZooKeeper/KRaft tuning, low memory). Apache Kafka or AWS MSK
remain drop-in for parity tests or cloud. Topics are versioned
(`sentinel.<domain>.v1`, see `sentinel_contracts.Topic`) with `.dlq` dead-letter twins.
Delivery is at-least-once; consumers deduplicate on `event_id`.

## Alternatives
- *Apache Kafka (KRaft)*: reference implementation, heavier on memory/startup.
- *Postgres-only queue (LISTEN/NOTIFY, SKIP LOCKED)*: fewer moving parts, but loses the
  replayable log and the distributed-systems story the project is meant to show.
- *NATS JetStream / RabbitMQ*: fine brokers, but not the Kafka API.

## Consequences
+ Fast local startup; same client code against MSK later.
− Redpanda-specific behaviour (e.g. defaults, admin API) must not leak into code.
− At-least-once means idempotency is mandatory in every consumer.
