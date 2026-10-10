# ADR-008: Deterministic rules provider by default; Claude optional

**Status:** Accepted · 2026-10-09

## Context
CI and local demos must be deterministic and free. The blueprint also asks to compare a
rules-only baseline against AI investigation.

## Decision
- `ModelProvider` interface with two implementations:
  - **rules** (default): deterministic playbook over evidence signals. Doubles as the
    rules-only baseline in evaluation.
  - **claude** (`SENTINEL_LLM_PROVIDER=claude`): `claude-opus-5` with adaptive thinking,
    strict tool schemas, runbook ids constrained to the allowlist enum, telemetry wrapped as
    untrusted data, server-side refusal fallbacks (`fallbacks: "default"`), and a bounded
    manual tool loop. Any failure degrades to a `failed` investigation and NEEDS_HUMAN.
- Tests use a scripted provider to simulate confused or malicious model output.
- `scripts/evaluate.py` runs fixtures for any set of providers; Claude runs are opt-in
  because they cost money.

## Consequences
+ Zero-cost, reproducible CI; the safety layers are tested independently of model quality.
− The rules baseline was written alongside the fixtures, so fixture pass rates measure
  plumbing and safety behaviour, not diagnostic skill. Live-scenario evaluation (Phase 6)
  is the meaningful measure.
