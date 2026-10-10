# ADR-011: Authentication and authorization

**Status:** Accepted · 2026-10-09

## Context
The API exposes incident data and, from Phase 4, approval and execution endpoints. The
blueprint asks for JWT/OIDC users, scoped service tokens for ingest, and roles
viewer/operator/approver/admin.

## Decision
- **Users:** bearer JWTs, verified with a pinned algorithm, issuer and audience. Locally we
  sign HS256 tokens with a random per-machine secret and expose a **dev login** for five
  demo identities (`AUTH_DEV_LOGIN=true`, local only). The verifier is a single class so a
  JWKS/OIDC verifier can replace it without touching endpoints.
- **Authorization:** roles map to explicit permissions (`read`, `investigate`,
  `request_approval`, `decide_approval`, `execute`, `run_experiments`, `admin`). Endpoints
  depend on permissions only. Operators cannot decide approvals; approvers cannot request or
  execute (separation of duties). Approval decisions additionally require
  approver ≠ requester (Phase 4).
- **Machines:** the ingestion gateway accepts a dedicated bearer token (random, mounted as a
  Compose secret, read via `*_FILE`). It has no other privileges.
- **SSE:** browsers cannot set headers on `EventSource`, so the stream endpoint also accepts
  `?access_token=`; tokens are short-lived and the API is loopback-only locally.

## Alternatives
- *Keycloak/Dex in Compose*: real OIDC flows, +1 heavy container and setup friction for a
  demo; documented as the hosted-deployment path.
- *Sessions/cookies*: needs CSRF protection; bearer tokens are simpler for an SPA + API.

## Consequences
+ RBAC is enforced and testable today.
− Dev login must be disabled in any shared deployment (Helm chart defaults it off).
