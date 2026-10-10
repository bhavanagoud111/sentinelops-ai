# Threat Model (v1 — Phases 0–7)

Method: assets → trust boundaries → STRIDE-style threats → mitigations, each marked
**built** (in code, with a test where noted), **partial**, or **gap**. Only controls that
exist in the repository are listed as built. Revisit at every milestone.

## Assets
1. Ability to change infrastructure (executor ServiceAccount) — **highest value**.
2. Incident, approval, execution and audit records (integrity, non-repudiation).
3. Telemetry content (logs/traces may contain PII or secrets).
4. Credentials: ingest token, JWT signing secret, LLM API key, kubeconfigs, DB password.

## Trust boundaries
| # | Boundary |
|---|---|
| B1 | Host ↔ local stack (published ports) |
| B2 | Alertmanager → ingestion gateway |
| B3 | Telemetry content → investigator → LLM prompt (untrusted data into a model) |
| B4 | Model output → proposal → approval → policy engine → executor → Kubernetes API |
| B5 | Browser user → dashboard → API |
| B6 | Services → Postgres / Redpanda (data plane) |

## Threats and mitigations

### B1 — local exposure
| Threat | Mitigation | Status |
|---|---|---|
| Stack reachable from the LAN | Every published port binds `127.0.0.1`; kind API server and NodePorts too (`infra/kind/cluster.yaml`) | built |
| Container breakout / privilege | Python services + dashboard: uid 10001, `read_only`, `cap_drop: ALL`, `no-new-privileges`; kind pods: `restricted` Pod Security, seccomp `RuntimeDefault`, no SA token automount | built (third-party images in Compose are not hardened — gap) |
| Unauthorised fault injection | Demo `/admin/faults` needs `X-Fault-Token` (constant-time); router not mounted without a token; fault injector needs a service bearer token; scenarios A–E are fixed | built |
| Secrets in Git | `.env`, `.cache/` git-ignored; gitleaks (full history) in `security.yml` | built |

### B2 — alert ingestion
| Threat | Mitigation | Status |
|---|---|---|
| Forged alerts | Shared bearer token, constant-time compare, ≥16 chars enforced at startup | built (`test_ingestion_abuse.py`) |
| Replayed alerts | Deterministic `event_id` = hash(source, fingerprint, startsAt, status) + unique constraint ⇒ a replay is a no-op. No HMAC signature or timestamp window | partial |
| Oversized / malformed payloads | 1 MB limit (Content-Length, then actual body); webhook v4 only; ≤500 alerts, ≤64 labels, value/key truncation, identifier coercion, `extra="forbid"` contracts | built (tested). Chunked bodies are buffered before the size check — gap |
| Flooding | Per-client token bucket (20/s, burst 100), 429 + `Retry-After` | built, per replica only |

### B3 — telemetry into the model
| Threat | Mitigation | Status |
|---|---|---|
| Prompt injection via logs/traces | System prompt declares telemetry untrusted; evidence wrapped in `<untrusted_telemetry>`; model sees bounded features, not raw dumps; strict tool schema with runbook id as an **enum** of the catalog; Pydantic re-validation | built (`test_llm_adversarial.py`, fixture `N2_prompt_injection_in_logs`) |
| Fabricated evidence | Content-addressed evidence ids; deterministic critic rejects unknown ids and out-of-scope services, requires an anomalous cited item; model critic can only lower confidence; proposal only at ≥0.6 | built (tested) |
| Runaway tool use / cost | ≤40 collection calls, ≤4 model follow-ups, 120 s deadline, query guards (≤6 h range, ≥5 s step, exact service matchers, result caps) | built (tested) |
| Secret/PII leakage to provider | Regex redaction (bearer/api keys, AWS/`sk-` keys, JWTs, emails, card-like numbers), line truncation; deterministic `rules` provider is the default — Claude only with `SENTINEL_LLM_PROVIDER=claude` | partial (regex redaction is best-effort) |
| Model failure misread as success | Refusal / max_tokens / any exception ⇒ FAILED, no proposal ⇒ incident NEEDS_HUMAN; orphan sweeper | built (tested) |

### B4 — proposal to cluster change (core invariant)
| Threat | Mitigation | Status |
|---|---|---|
| Model triggers an arbitrary action | No command/manifest path exists: four allowlisted runbooks, each one fixed JSON-patch template; the API never touches the cluster | built |
| Action without human consent | Approval required; four-eyes (approver ≠ requester) enforced in API and re-checked by the executor; approval expires (15 min default) | built (tested) |
| Approved action swapped for another | Approval bound by SHA-256 request hash (incident, runbook, params, target revision); executor recomputes; `no_revision_drift` vs. live deployment | built (tested) |
| Policy bypass at time of use | Executor re-runs the full policy engine against a live snapshot: mode = `sandbox`, allowlist, params, namespace, deployment, approval checks, incident state, target exists, rollback target, scale bounds (1–`scaleMax`, default 4), flag allowlist, no concurrent action, 300 s cooldown | built (`test_policy_engine.py`). No blast-radius check — gap |
| Double execution | Idempotency-Key required on execute; unique `executions.approval_id`; partial unique indexes (one live approval per incident, one running execution per target); row locks; approval consumed on use | built |
| Over-privileged credentials | Namespaced Roles only: reader = get/list/watch; executor = + `patch` deployments. No create/delete/exec/secrets; `kind-up.sh` asserts this with `kubectl auth can-i`. Helm chart mirrors it and mounts a token only on reader/executor pods | built. Local demo uses long-lived SA token Secrets — accepted (local) |
| Remediation on by default | Default mode `recommend_only`; writes need `REMEDIATION_MODE=sandbox` **and** the approval chain. No autonomous mode exists | built |
| Unverified "fix" | Independent verifier: 2 consecutive healthy checks, alert clearing alone is not recovery | built |

### B5 — users
| Threat | Mitigation | Status |
|---|---|---|
| Broken access control | HS256 JWT (alg pinned, issuer + audience checked), roles viewer/operator/approver/admin, permission per route | built (`test_authz_matrix.py`, forged-token tests) |
| Weak login | Dev login (username only) for five demo users, gated by `AUTH_DEV_LOGIN`: code default false, **Compose default true**, Helm default false. No OIDC yet | gap for any hosted use |
| Token theft | 8 h TTL, no revocation; token in `localStorage`; `?access_token=` accepted **only** on the SSE stream (browsers' EventSource cannot set headers) | partial |
| XSS / clickjacking | nginx: CSP `default-src 'self'`, `frame-ancestors 'none'`, `X-Frame-Options DENY`, `nosniff`, no Referer; React escapes output | built. No TLS/HSTS locally |
| CSRF | Bearer header, no cookies | n/a by design |

### B6 — data plane and audit
| Threat | Mitigation | Status |
|---|---|---|
| Audit tampering | Hash chain (`sha256(prev + canonical row)`, serialized by advisory lock); row trigger rejects UPDATE/DELETE | partial: all services share one DB superuser that could drop the trigger; TRUNCATE not blocked; chain verified only in tests, not at runtime; no external anchor |
| Event duplication / loss | Transactional outbox; consumer dedup table `processed_events`; replay-safe correlator | built (integration-tested) |
| Unauthenticated broker / backends | Redpanda dev mode without auth/TLS; Prometheus lifecycle API open; Loki/Tempo/Alertmanager open — all loopback/Compose-network only | accepted (local) |

### Supply chain
| Threat | Mitigation | Status |
|---|---|---|
| Vulnerable dependencies | `uv.lock` + `pip-audit --strict` weekly and on push; pinned image tags | partial: image scan (Trivy) and SBOM/provenance in the release pipeline, not yet exercised by a tagged release; no npm audit, no image signing; Actions pinned by tag, not SHA |

## Accepted risks (local demo only)
- Grafana anonymous Viewer, admin password defaults to `admin`.
- Postgres password defaults to `sentinel`; one shared DB account.
- Generated secrets and kubeconfigs under `.cache/` are mode 644 (single-user laptop).
- Fault-admin token defaults to `local-dev-fault-token`.
- No TLS on internal hops.

Every item above must be closed before a hosted deployment; the Helm chart already
closes dev login, token automount, and ingress exposure by default.

## Backlog (ordered by risk reduction)
1. Separate DB roles: services get INSERT/SELECT on `audit_log`, no DDL; owner role only for migrations.
2. Periodic audit-chain verification job + metric/alert; anchor chain head externally.
3. OIDC login; shorter JWT TTL; token revocation.
4. HMAC-signed ingress with timestamp window.
5. npm audit and signed images; pin GitHub Actions by SHA; cut a first tagged release.
6. Blast-radius policy check (e.g. max share of replicas affected).
