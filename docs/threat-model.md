# Threat Model (v0 — Phase 0/1)

Scope grows per phase; revisit at each milestone gate. Method: assets → trust boundaries →
STRIDE-style threats → mitigations (built / planned).

## Assets
1. Ability to change infrastructure (Phase 4 executor) — **highest value**.
2. Incident, approval and audit records (integrity, non-repudiation).
3. Telemetry content (may contain PII/secrets in logs).
4. Credentials: ingest tokens, user identities, LLM API keys, kubeconfig.

## Trust boundaries
| # | Boundary | Phase |
|---|---|---|
| B1 | Host ↔ Compose stack (published ports) | 1 |
| B2 | Alertmanager → ingestion gateway webhook | 2 |
| B3 | Telemetry content → LLM prompt (untrusted data into model) | 3 |
| B4 | Model output → policy engine → executor → Kubernetes API | 4 |
| B5 | Browser user → API (authn/authz) | 5 |

## Threats and mitigations
| Threat | Boundary | Mitigation | Status |
|---|---|---|---|
| Stack reachable from LAN | B1 | All ports bind `127.0.0.1` | built |
| Unauthorised fault injection | B1 | `/admin/faults` requires `X-Fault-Token` (constant-time compare); router not mounted when token unset; fault names allowlisted; parameters bounded | built |
| Container breakout / privilege | B1 | demo images run as uid 10001, `read_only`, `cap_drop: ALL`, `no-new-privileges` | built |
| Secrets in Git | all | `.env` git-ignored, `.env.example` has dev-only values; CI secret scan | built / CI |
| Forged or replayed alerts | B2 | shared secret / signed ingress, schema validation, deterministic `event_id` dedup, rate limit | planned P2 |
| Oversized / malicious payloads | B2 | contract bounds (label counts, string lengths, identifier pattern) | contracts built |
| Prompt injection via logs/traces | B3 | tool output wrapped as data, never instructions; structured output schema; critic step; evidence citations required | planned P3 |
| Secret/PII leakage to model provider | B3 | redaction in tools/collector; mock provider default; data-boundary doc | planned P3 |
| Model triggers unsafe action | B4 | no free-form commands; allowlisted runbooks; human approval bound to incident+action+hash with expiry; policy checks (namespace, RBAC, cooldown, concurrency); least-privilege ServiceAccount; dry-run | planned P4 |
| Double execution | B4 | optimistic locking (`version`), idempotency keys, state machine forbids REJECTED→REMEDIATING | state machine built |
| Audit tampering | B4/B5 | append-only audit, optional hash chain | planned P4/P6 |
| Broken access control in UI/API | B5 | OIDC/JWT, roles viewer/operator/approver/admin | planned P5 |
| Supply chain | all | pinned images and `uv.lock`; dependency + image scanning in CI | partial |

## Accepted risks (local demo)
- Grafana admin password defaults to `admin` and anonymous Viewer is enabled — acceptable
  only because ports are loopback-bound. Hosted deployments must change both.
- Telemetry data is unauthenticated inside the Compose network.
