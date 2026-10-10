#!/usr/bin/env bash
# Install / uninstall the Helm chart into the local kind cluster (Phase 7 test install).
#   scripts/helm-test.sh install     stop the Compose pipeline, install the chart, wait
#   scripts/helm-test.sh smoke       post one synthetic alert through the Helm pipeline
#   scripts/helm-test.sh uninstall   remove the release and restart the Compose pipeline
# The Helm release reuses Compose Postgres/Redpanda/observability, so the Compose
# pipeline is stopped while it runs (otherwise both would share Kafka consumer groups).
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$PWD/.cache/bin:$PATH:/Applications/Docker.app/Contents/Resources/bin"
export KUBECONFIG="$PWD/.cache/kube/admin.kubeconfig"
CLUSTER=sentinelops
NS=sentinelops-system
RELEASE=sentinelops
PIPELINE=(api ingestion relay correlator investigator executor verifier)
COMPOSE=(docker compose -f infra/compose/docker-compose.yml)

install() {
  "${COMPOSE[@]}" stop "${PIPELINE[@]}"
  kind load docker-image sentinelops/platform:dev sentinelops/dashboard:dev --name "$CLUSTER" >/dev/null
  kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
  kubectl label ns "$NS" pod-security.kubernetes.io/enforce=restricted --overwrite >/dev/null
  POSTGRES_PASSWORD="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- || true)"
  kubectl -n "$NS" create secret generic sentinelops --dry-run=client -o yaml \
    --from-literal=database-url="postgresql+asyncpg://sentinel:${POSTGRES_PASSWORD:-sentinel}@postgres:5432/sentinelops" \
    --from-file=jwt-secret=.cache/secrets/jwt_secret \
    --from-file=ingest-token=.cache/secrets/ingest_token | kubectl apply -f - >/dev/null
  helm upgrade --install "$RELEASE" infra/helm/sentinelops -n "$NS" \
    -f infra/helm/sentinelops/values-kind.yaml --wait --timeout 5m
  kubectl -n "$NS" get pods -o wide
}

smoke() {
  local port=18901 token now fp
  kubectl -n "$NS" port-forward "svc/$RELEASE-ingestion" "$port:8000" >/dev/null 2>&1 &
  local pf=$!
  trap 'kill $pf 2>/dev/null' RETURN
  sleep 3
  token="$(cat .cache/secrets/ingest_token)"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fp="helmsmoke$(date +%s)"
  curl -sS -f -X POST "http://127.0.0.1:$port/api/v1/ingest/alertmanager" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d @- <<JSON
{"version":"4","status":"firing","receiver":"sentinelops","groupKey":"helm-smoke",
 "alerts":[{"status":"firing","fingerprint":"$fp","startsAt":"$now",
   "labels":{"alertname":"HelmSmokeTest","service":"helm-smoke","severity":"warning"},
   "annotations":{"summary":"synthetic alert from scripts/helm-test.sh"}}]}
JSON
  echo
}

uninstall() {
  helm uninstall "$RELEASE" -n "$NS" --wait 2>/dev/null || true
  kubectl delete ns "$NS" --ignore-not-found >/dev/null
  make compose-up >/dev/null
  echo "Compose pipeline restarted"
}

case "${1:-}" in
  install) install ;;
  smoke) smoke ;;
  uninstall) uninstall ;;
  *) echo "usage: $0 install|smoke|uninstall"; exit 2 ;;
esac
