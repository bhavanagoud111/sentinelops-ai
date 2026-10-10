# ADR-010: Incident correlation key and reopen policy

**Status:** Accepted · 2026-10-09

## Context
The correlator must turn a stream of alert notifications (with Alertmanager resends,
resolved notifications and replays) into a small number of meaningful incidents. The
blueprint leaves the grouping key and reopen behaviour open.

## Decision
- **Correlation key** = `environment:service:alertname` (`AlertEvent.fingerprint`). The
  Alertmanager fingerprint (all labels) is kept as `labels.am_fingerprint` for traceability
  but is too fine-grained (instance/pod labels) to group on.
- **Dedup** happens twice: the gateway's unique `event_id` (source fingerprint + startsAt +
  status) absorbs webhook resends; the correlator's `processed_events` ledger absorbs Kafka
  redelivery and replays. A partial unique index guarantees at most one non-resolved
  incident per key even under concurrent consumers.
- **firing** → attach to the open incident for the key, else **reopen** the most recently
  resolved one if it resolved ≤ 30 min before the new alert started
  (`REOPEN_WINDOW_MINUTES`), else create a new incident.
- **resolved** → mark `alert_active=false` on the open incident. Incidents are closed only by
  the verifier or a human — a cleared alert is evidence, not proof, of recovery.
- Severity only escalates while an incident is open.

## Alternatives
- *Time-window grouping across alertnames per service*: catches multi-symptom incidents
  but merges unrelated failures; deferred to the investigator, which can link incidents.
- *Auto-resolve on alert resolution*: simpler, but defeats verification and hides flapping.

## Consequences
+ Deterministic, explainable grouping; replay-safe.
− Cross-service cascades (scenario C) produce one incident per symptomatic service;
  the investigator is responsible for identifying the shared root cause.
