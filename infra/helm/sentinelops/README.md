# SentinelOps Helm chart

Deploys the SentinelOps control plane: API, ingestion gateway, outbox relay,
correlator, investigator, executor, verifier, dashboard, plus a pre-install /
pre-upgrade migration Job. Postgres, Kafka and the observability backends are
**external** and addressed by URL (`config.*`); in AWS that is RDS + MSK (see
`infra/terraform`). The demo fault injector is intentionally not packaged.

## Safety defaults
| Setting | Default | Why |
|---|---|---|
| `config.remediationMode` | `recommend_only` | Writes need `sandbox` **and** an approved, policy-checked runbook. No autonomous mode exists. |
| `config.authDevLogin` | `false` | Dev login takes a username only. |
| `config.llmProvider` | `rules` | Deterministic, free; `claude` needs `anthropic-api-key`. |
| RBAC | namespaced Roles in `policy.namespace` | Mirrors `infra/kind/rbac.yaml`: reader = get/list/watch; executor = + `patch` deployments. No create/delete/exec/secrets, nothing cluster-scoped. |
| ServiceAccount tokens | mounted only on investigator, verifier (reader) and executor | Other pods get no API token. |
| Pod security | `restricted` profile | non-root uid 10001, read-only root FS, all capabilities dropped, seccomp `RuntimeDefault`. |
| NetworkPolicy | ingress default-deny | Only dashboard → API, configured frontends → dashboard/API, configured sources → ingestion. Egress allowlist is opt-in (`networkPolicy.egress`). |
| Secrets | `existingSecret` | The chart never generates credentials; each pod mounts only the keys it uses. |

## Required Secret
```bash
kubectl -n sentinelops-system create secret generic sentinelops \
  --from-literal=database-url='postgresql+asyncpg://USER:PASS@HOST:5432/sentinelops' \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=ingest-token="$(openssl rand -hex 32)"
# optional: --from-literal=anthropic-api-key=...   (only with config.llmProvider=claude)
```
Point Alertmanager's webhook at
`http://<release>-sentinelops-ingestion.<ns>.svc:8000/api/v1/ingest/alertmanager` with
`Authorization: Bearer <ingest-token>`.

## Validate
`make helm-check` — `helm lint --strict` plus offline schema validation (kubeconform) of
the default render and of `ci/all-features-values.yaml`.

## Local test install (kind)
Reuses the Compose Postgres/Redpanda/observability. Stop the Compose pipeline first so
two control planes don't split the same Kafka consumer groups:
```bash
make helm-test-install     # stops Compose pipeline, loads images, installs, waits
make helm-test-uninstall   # uninstalls and restarts the Compose pipeline
```

## Known limitations
- Pipeline workers (relay, correlator, investigator, executor, verifier) expose no
  health endpoint, so they have no probes; Kubernetes restarts them on crash only.
- kind's default CNI (kindnet) does not enforce NetworkPolicy; use Calico/Cilium (or
  EKS with the VPC CNI network-policy agent) for enforcement.
- The ingestion rate limiter is per replica (in-process).
