# ADR-007: Docker Compose first, kind for Kubernetes workloads in Phase 4

**Status:** Accepted · 2026-10-09

## Context
The spec runs the control plane in Compose and demo workloads in kind. Running both from
Phase 1 means solving host↔cluster networking (Prometheus scraping into kind, executor
reaching the kind API) before anything needs Kubernetes.

## Decision
- Phases 0–3: everything, including demo services, runs in **one Compose project**
  (`sentinelops`) on a private network; host ports bind to 127.0.0.1 only.
- Phase 4: demo workloads move to a **kind** cluster because the executor needs real
  Deployments to restart/rollback/scale. The OTel Collector gets an in-cluster agent that
  forwards to the Compose collector; the executor uses a kubeconfig scoped to a dedicated
  ServiceAccount in the `sentinelops-demo` namespace.
- If host↔cluster wiring becomes painful, move the control plane into kind (spec §18).

## Alternatives
- *kind from day one*: closer to the final topology, slower feedback for Phases 1–3.
- *k3d*: similar; kind chosen for ubiquity in CI.

## Consequences
+ Phases 1–3 need only Docker.
− Fault scenario D (CrashLoopBackOff) cannot be demonstrated until Phase 4.
− Some Phase 1 service config moves to Helm/manifests later.
