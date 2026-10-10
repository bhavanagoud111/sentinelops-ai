#!/usr/bin/env bash
# Create/refresh the kind cluster for the demo workload (ADR-007).
# Idempotent. Writes only inside the repo (.cache/kube) and to Docker.
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$PWD/.cache/bin:$PATH:/Applications/Docker.app/Contents/Resources/bin"
export KUBECONFIG="$PWD/.cache/kube/admin.kubeconfig"
CLUSTER=sentinelops
NODE="${CLUSTER}-control-plane"
NS=sentinelops-demo
mkdir -p .cache/kube

for bin in kind kubectl docker; do
  command -v "$bin" >/dev/null || { echo "missing $bin (run: make tools)"; exit 1; }
done

docker network inspect sentinelops >/dev/null 2>&1 || docker network create sentinelops >/dev/null

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --config infra/kind/cluster.yaml --kubeconfig "$KUBECONFIG"
fi
kind get kubeconfig --name "$CLUSTER" > "$KUBECONFIG"
# Let the node (and therefore pods) resolve and reach Compose services by name.
docker network connect sentinelops "$NODE" 2>/dev/null || true
kubectl wait --for=condition=Ready node --all --timeout=180s >/dev/null

echo "==> building and loading demo images"
for svc in checkout inventory payment worker loadgen; do
  docker build -q -f infra/docker/python-service.Dockerfile --build-arg "PACKAGE=demo-$svc" \
    -t "sentinelops/demo-$svc:dev" . >/dev/null
  kind load docker-image "sentinelops/demo-$svc:dev" --name "$CLUSTER" >/dev/null
done

echo "==> applying namespace, RBAC, kube-state-metrics"
kubectl apply -f infra/kind/rbac.yaml >/dev/null
kubectl apply -f infra/kind/kube-state-metrics.yaml >/dev/null
kubectl -n "$NS" create secret generic demo-fault-token \
  --from-literal=token="${FAULT_ADMIN_TOKEN:-local-dev-fault-token}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "==> applying demo workload"
kubectl apply -f infra/kind/workloads.yaml >/dev/null
# Images keep the :dev tag, so a rebuilt image does not change the pod template.
# Restart so pods pick up the freshly loaded images.
kubectl -n "$NS" rollout restart deployment/checkout deployment/inventory deployment/payment \
  deployment/worker deployment/loadgen >/dev/null
for d in checkout inventory payment worker loadgen kube-state-metrics; do
  kubectl -n "$NS" rollout status "deployment/$d" --timeout=180s >/dev/null
done

echo "==> writing scoped kubeconfigs (server reachable from the Compose network)"
CA=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
for sa in reader executor chaos; do
  for _ in $(seq 1 30); do
    TOKEN=$(kubectl -n "$NS" get secret "sentinel-$sa-token" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
    [[ -n "$TOKEN" ]] && break
    sleep 1
  done
  [[ -n "$TOKEN" ]] || { echo "token for sentinel-$sa not issued"; exit 1; }
  cat > ".cache/kube/$sa.kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: sentinelops
    cluster:
      server: https://$NODE:6443
      certificate-authority-data: $CA
users:
  - name: sentinel-$sa
    user:
      token: $TOKEN
contexts:
  - name: sentinel-$sa
    context: { cluster: sentinelops, user: sentinel-$sa, namespace: $NS }
current-context: sentinel-$sa
EOF
  chmod 644 ".cache/kube/$sa.kubeconfig"
done

echo "==> verifying least privilege"
expect() { # expect <yes|no> <verb> <resource> <namespace> <sa>
  local got
  got=$(kubectl auth can-i "$2" "$3" -n "$4" --as "system:serviceaccount:$NS:sentinel-$5" 2>/dev/null || true)
  [[ "$got" == "$1" ]] || { echo "RBAC check failed: sentinel-$5 $2 $3 in $4 -> $got (want $1)"; exit 1; }
}
expect no  patch  deployments "$NS" reader
expect yes list   pods        "$NS" reader
expect yes patch  deployments "$NS" executor
expect no  delete deployments "$NS" executor
expect no  create pods/exec   "$NS" executor
expect no  get    secrets     "$NS" executor
expect no  patch  deployments kube-system executor
expect no  delete deployments "$NS" chaos
echo "kind cluster ready: kubectl --kubeconfig .cache/kube/admin.kubeconfig -n $NS get pods"
