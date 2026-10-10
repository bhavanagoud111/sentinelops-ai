# ADR-006: Human approval and allowlisted Kubernetes runbooks

**Status:** Accepted · 2026-10-10

## Context
The core invariant: an LLM can analyze and recommend but must never independently execute
infrastructure operations. Remediation still has to be fast enough to matter.

## Decision
- **Allowlist.** Four runbooks only (`restart`, `rollback`, `scale` within 1–4, `disable
  FEATURE_* flag`), each a typed, bounded parameter model and a fixed JSON-patch template on a
  single Deployment in `sentinelops-demo`. No runbook accepts commands, manifests or kinds.
- **Two-stage control.** (1) *Approval*: an operator requests it from an investigation
  proposal; a *different* approver decides (four-eyes enforced in the API, the policy engine
  and a DB check constraint). Approvals expire (15 min) and are bound by
  `request_hash = sha256(incident, runbook, normalized params, observed revision)`.
  (2) *Policy*: the executor re-evaluates 15+ checks against a live deployment snapshot right
  before writing — mode, allowlist, params, namespace, deployment, approval state/expiry/
  binding, four-eyes, incident state, revision drift, rollback target, scale bounds, flag
  allowlist and state, concurrency, cooldown. All results are stored on the execution.
- **Least privilege.** The executor's ServiceAccount can only get/list/patch deployments
  (and read replicasets/pods) in the demo namespace — verified by `kubectl auth can-i` on
  every `make kind-up`. Server-side `dryRun=All` precedes each write.
- **Modes.** `REMEDIATION_MODE` = `disabled` | `recommend_only` (platform default) | `sandbox`.
  Only `sandbox` writes. The local demo opts into sandbox via `.env`.
- **Verification.** A separate verifier re-measures the incident's symptom and pod health
  and requires consecutive healthy checks before RESOLVED; otherwise FAILED → human.
- **Audit.** Every request, decision, policy result, command and before/after state is in the
  append-only, hash-chained audit log.

## Alternatives
- *Auto-remediate high-confidence diagnoses*: faster, violates the core invariant.
- *GitOps PRs as the action*: safest for production; slower for a live demo. A natural
  hosted-mode extension (the executor would open a PR instead of patching).

## Consequences
+ An adversarial model output cannot reach the cluster without a human and every check.
− Two humans are needed for every action (dev identity `sam.sre` holds both roles but still
  cannot approve their own request).
