# ADR-012: Helm chart for the control plane; AWS as optional, validated-only Terraform

**Status:** Accepted · 2026-10-10

## Context
Spec §18 asks for Helm charts (NetworkPolicies, dedicated ServiceAccounts, limits,
probes, HPA) and optional Terraform for VPC/EKS/RDS/ECR with documented cost and
teardown, without making AWS a requirement. Locally the control plane runs in Compose
and reaches the kind cluster through per-ServiceAccount kubeconfig files.

## Decision
- One chart, `infra/helm/sentinelops`, for the control plane only. Postgres, Kafka and
  the observability backends are **external dependencies addressed by URL**, so the same
  chart runs against Compose (local test install), RDS/MSK, or in-cluster operators.
- Kubernetes access in-cluster uses the pod's own ServiceAccount: services accept
  `K8S_*_KUBECONFIG=in-cluster`. Only investigator/verifier (reader) and executor pods
  mount a token; Roles are namespaced and identical to `infra/kind/rbac.yaml`.
- The chart never generates secrets (`existingSecret`), defaults to `recommend_only`,
  dev login off, ingress default-deny NetworkPolicies, `restricted` pod security.
- The demo fault injector is not packaged (it holds a write-capable chaos identity).
- Terraform (`infra/terraform`) is written and checked with `fmt`/`validate` in CI, never
  applied by automation. Kafka defaults to in-cluster Redpanda; MSK is opt-in
  (provisioned, because the services do not implement MSK Serverless IAM auth).
- Releases (`.github/workflows/release.yml`) build images with SBOM + provenance, scan
  them, and package the chart pinned to the same tag. Nothing deploys automatically.

## Alternatives
- *Bundle Postgres/Redpanda subcharts*: one-command installs, but couples stateful
  services to the app's release cycle and hides the cost of running them.
- *Kustomize*: fine for fixed environments; Helm chosen for value-driven toggles
  (HPA, ingress, egress policy) and a versioned release artifact.
- *IRSA / cloud IAM for Kubernetes access*: not needed — the services talk only to the
  Kubernetes API, which ServiceAccount RBAC already scopes.

## Consequences
+ Same least-privilege model locally and in-cluster; one small code path added.
+ AWS cost is opt-in and documented before anyone applies.
− Pipeline workers have no health endpoints, so no probes (crash-restart only).
− kind's default CNI does not enforce NetworkPolicy; enforcement is verified only by
  rendering, not locally.
