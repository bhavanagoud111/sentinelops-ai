# Deploying SentinelOps to your own AWS account

This guide takes you from an empty AWS account to SentinelOps watching **your** services
on EKS. Read it end to end once before running anything: step 1 creates billable resources
(≈ $188/month at the defaults — an estimate, see [`infra/terraform/README.md`](../infra/terraform/README.md)).

> Every artifact here has been validated (`terraform validate`, `helm lint`, schema
> validation, a live Helm install into kind), but this Terraform has **not** yet been
> applied to a real AWS account. Treat the first run as a staging exercise.

```
             your EKS cluster
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ ns: shop (your apps)        ns: observability            ns: sentinelops-system
 │  web, api, worker  ──OTLP──▶ otel-collector ─▶ Prometheus ─▶ Alertmanager ─┐
 │        ▲                    Loki · Tempo · Grafana                         │
 │        │ patch (allowlisted, approved)          ingestion ◀── webhook ─────┘
 │        └──────────────────────────────── executor ◀─ API ◀─ dashboard (ALB)
 └────────────────────────── RDS Postgres · Redpanda/MSK ───────────────────┘
```

## 0. Prerequisites
AWS account and CLI credentials with admin rights for the first apply; `terraform` ≥ 1.9,
`kubectl`, `helm` ≥ 3.14, `docker` with buildx, `psql`. A domain + ACM certificate if you
want the dashboard behind HTTPS (recommended).

## 1. Infrastructure (Terraform)
```bash
cd infra/terraform/envs/demo
cp terraform.tfvars.example terraform.tfvars   # region, your IP for the EKS API, budget email
terraform init
terraform plan -out tf.plan                   # read it
terraform apply tf.plan
$(terraform output -raw kubeconfig_command)   # kubectl now talks to the new cluster
```
Creates: VPC (2 AZ), EKS + node group, RDS Postgres 17, ECR repos, optional MSK, budget.
Install the [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/)
if you want the ALB ingress in step 6.

## 2. Images
EKS nodes are x86_64, so build for `linux/amd64`:
```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text); REGION=us-east-1
REG=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinelops; TAG=v0.1.0
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${REG%%/*}
docker buildx build --platform linux/amd64 -f infra/docker/python-service.Dockerfile \
  --build-arg PACKAGE=sentinel-platform -t $REG/platform:$TAG --push .
docker buildx build --platform linux/amd64 -f infra/docker/dashboard.Dockerfile \
  -t $REG/dashboard:$TAG --push .
```
(Or tag `vX.Y.Z` on GitHub: `.github/workflows/release.yml` publishes scanned images with
SBOMs to GHCR and attaches the packaged chart to the release.)

## 3. Kafka and observability
SentinelOps consumes these; it does not install them.

| Need | Suggested chart | Notes |
|---|---|---|
| Kafka API | [Redpanda](https://docs.redpanda.com/current/deploy/deployment-option/self-hosted/kubernetes/) in ns `redpanda`, or `kafka_mode = "msk"` | Plain Kafka protocol inside the VPC; topics are created by the services on start |
| Metrics + alerting | `prometheus-community/kube-prometheus-stack` in ns `observability` | Load the rules below |
| Logs / traces | `grafana/loki`, `grafana/tempo` | Must be queryable by `service_name` |
| Collector | `open-telemetry/opentelemetry-collector` | Pipeline as in `infra/observability/otel-collector/config.yaml` |

**Your services must be instrumented with OpenTelemetry** (HTTP server metrics,
logs, traces) with `service.name` = the Kubernetes Deployment name. That name is the key
SentinelOps uses to join alerts, metrics, logs, traces and the Deployment to act on.

Rules:
- Load the recording rules from `infra/observability/prometheus/rules/demo-services.yml`
  (group `demo-services.recording` — despite the file name these are service-agnostic)
  and `platform.yml`.
- Write alerts for your services with a **`service` label** equal to the Deployment name:
  ```yaml
  - alert: HighErrorRate
    expr: service:http_errors_5xx:ratio1m{service_name="web"} > 0.05
    for: 2m
    labels: { severity: critical, service: web }
    annotations: { summary: "web 5xx ratio above 5%" }
  ```
- Route Alertmanager to SentinelOps (token from step 4):
  ```yaml
  receivers:
    - name: sentinelops
      webhook_configs:
        - url: http://sentinelops-ingestion.sentinelops-system.svc:8000/api/v1/ingest/alertmanager
          send_resolved: true
          http_config:
            authorization: { type: Bearer, credentials_file: /etc/alertmanager/secrets/sentinelops-ingest/token }
  ```

## 4. Database user and the Secret
Use a dedicated application role, not the RDS master user (threat model backlog #1):
```bash
MASTER=$(aws secretsmanager get-secret-value --secret-id "$(terraform -chdir=infra/terraform/envs/demo output -raw postgres_master_secret_arn)" --query SecretString --output text | jq -r .password)
HOST=$(terraform -chdir=infra/terraform/envs/demo output -raw postgres_endpoint)   # host:port
APP_PW=$(openssl rand -hex 24)
# from a pod or bastion inside the VPC:
PGPASSWORD=$MASTER psql "host=${HOST%:*} dbname=sentinelops user=sentinel_admin sslmode=require" <<SQL
CREATE ROLE sentinel_app LOGIN PASSWORD '$APP_PW';
GRANT ALL ON SCHEMA public TO sentinel_app;
SQL

kubectl create namespace sentinelops-system
kubectl label namespace sentinelops-system pod-security.kubernetes.io/enforce=restricted
INGEST=$(openssl rand -hex 32)
kubectl -n sentinelops-system create secret generic sentinelops \
  --from-literal=database-url="postgresql+asyncpg://sentinel_app:$APP_PW@$HOST/sentinelops?ssl=require" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=ingest-token="$INGEST"
kubectl -n observability create secret generic sentinelops-ingest --from-literal=token="$INGEST"
```
(Prefer the External Secrets Operator syncing from Secrets Manager for anything long-lived.)

## 5. Decide what SentinelOps may touch
Edit a copy of [`infra/helm/sentinelops/values-aws.example.yaml`](../infra/helm/sentinelops/values-aws.example.yaml):

```yaml
policy:
  namespace: shop                  # one namespace
  deployments: [web, api, worker]  # the only Deployments it can restart/roll back/scale
  featureFlags: []                 # FEATURE_* env flags it may switch off
  scaleMax: 6
```
This drives both the Kubernetes Roles (namespaced; get/list/watch, and `patch` on
Deployments for the executor — never create/delete/exec/secrets) and the allowlist every
service enforces. Runbook *types* are fixed in code; there is no free-form action.

Keep `config.remediationMode: recommend_only` at first: SentinelOps will diagnose and
propose, and the executor refuses every write. Switch to `sandbox` once you trust the
proposals — writes still need a second person's approval and pass the policy engine.

## 6. Install
```bash
helm upgrade --install sentinelops infra/helm/sentinelops -n sentinelops-system \
  -f my-values.yaml --wait
kubectl -n sentinelops-system get pods
```
A pre-install Job runs the database migrations. NOTES printed by Helm show the
webhook URL and the active safety settings.

## 7. Sign in
Dev login is off in production. An administrator with `kubectl exec` rights mints a
token per person (max 24 h):
```bash
kubectl -n sentinelops-system exec deploy/sentinelops-api -- \
  sentinel-issue-token --user alice@example.com --name "Alice" --role approver
```
Roles: `viewer` (read), `operator` (investigate, request approval, execute), `approver`
(approve/reject), `admin` (everything). The person opens the dashboard and pastes the
token. Restrict `pods/exec` in `sentinelops-system` — whoever can exec can mint tokens.
Single sign-on (OIDC) is on the roadmap.

## 8. Optional: register service metadata
Incidents work for any `service` label. For dependency-aware diagnosis and criticality,
add rows to the `services` table:
```sql
INSERT INTO services (name, namespace, owner, criticality, description, dependencies)
VALUES ('web', 'shop', 'team-web', 'critical', 'Storefront', '["api"]');
```

## 9. Verify
- Grafana → *SentinelOps Platform*: consumer lag, outbox backlog, investigation outcomes.
- Fire a test alert (`amtool alert add HighErrorRate service=web severity=warning`) and
  watch it become an incident with an investigation in the dashboard.

## 10. Upgrade and teardown
Upgrade: push new images, bump `image.*.tag`, `helm upgrade` (migrations run first).
Teardown: `helm uninstall sentinelops -n sentinelops-system`, then
`terraform destroy` — see the checklist in `infra/terraform/README.md`.

## Before calling it production
Review [`docs/threat-model.md`](threat-model.md). The open items that matter most for a
real deployment: OIDC instead of minted tokens, separate DB roles for the audit log, a
runtime audit-chain verifier, HMAC-signed alert ingestion, and TLS between services
(e.g. a service mesh).
