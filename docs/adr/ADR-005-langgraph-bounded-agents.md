# ADR-005: LangGraph workflow with bounded, typed, read-only tools

**Status:** Accepted · 2026-10-09

## Context
Investigations must be explainable, reproducible and safe. Free-form autonomous agents
(agents choosing their own next step, calling each other, or composing shell commands)
are hard to bound and audit.

## Decision
- A fixed LangGraph state machine: triage → collect_evidence → root_cause → critic →
  plan_remediation → report. The orchestrator owns control flow; there is no agent-to-agent
  delegation. Recursion limit 12, shared wall-clock deadline (default 120 s, enforced as a
  hard timeout), collector budget 40 queries, LLM follow-up budget 4 queries.
- Evidence collection is a deterministic plan (10 evidence kinds × services in scope). Tools
  are GET-only clients behind a circuit breaker with size caps; every query passes guards
  (exact service scope, ≤ 6 h range, min step, result limits) and results are redacted.
- Evidence is immutable and content-addressed (`ev_<sha256(...)>`). Hypotheses must cite
  evidence ids. A deterministic critic rejects fabricated or uncorroborated hypotheses and
  strips non-allowlisted / out-of-scope runbooks; an optional model critic can only lower
  confidence. The planner proposes at most one allowlisted runbook above confidence 0.6;
  otherwise the incident escalates (NEEDS_HUMAN) or the system abstains.
- Investigations run outside DB transactions; begin/finish are separate transactions, and a
  sweeper fails investigations orphaned by a crash (verified live).

## Alternatives
- *Autonomous ReAct agent with all tools*: more flexible, much harder to bound, test and
  explain; rejected for the safety-critical path.
- *Temporal* for orchestration: durable and excellent, but heavyweight for v1 (noted as a
  future option in the blueprint).

## Consequences
+ Every conclusion is traceable to stored evidence; behaviour is testable with fixtures.
− The fixed plan can miss signals outside its 10 kinds; the LLM follow-up step and
  `next_checks` partially compensate.
