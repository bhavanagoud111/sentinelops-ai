# ADR-009: Server-Sent Events before WebSockets

**Status:** Accepted · 2026-10-10

## Context
The dashboard must reflect incident, investigation, approval and execution changes live.
Updates flow one way (server → browser); user actions go through the REST API, which
enforces RBAC, idempotency and audit.

## Decision
- `GET /api/v1/events/stream` (SSE). Each API process runs one Kafka consumer with a
  unique group id and `latest` offsets over the four lifecycle topics and fans out to
  connected clients through bounded per-client queues (slow clients drop oldest).
- Messages are *notifications* (topic, kind, incident id, status) — the browser refetches
  via REST, so the stream never exposes more than the REST API would. Heartbeats every 15 s;
  `retry: 3000` for reconnects. Auth uses the bearer token (query parameter for
  `EventSource`, see ADR-011). nginx disables buffering on the stream location.

## Alternatives
- *WebSockets*: bidirectional but unnecessary; harder to proxy, no automatic reconnect.
- *Polling only*: simpler, but laggy or wasteful. Kept as the fallback (queries refetch on
  an interval when the stream is offline).

## Consequences
+ Simple, proxy-friendly, auto-reconnecting live UI.
− Per-process consumers mean N API replicas read each event N times (fine at demo scale).
