#!/usr/bin/env bash
# Inject a demo fault into the sandbox workload (kind, namespace sentinelops-demo).
#
#   scripts/inject-fault.sh A        checkout 5xx: rollout with a malformed PAYMENT_PROVIDER_URL
#   scripts/inject-fault.sh B        inventory latency: rollout enabling FEATURE_NEW_STOCK_INDEX
#   scripts/inject-fault.sh C        payment provider slowdown (runtime; external outage)
#   scripts/inject-fault.sh D        inventory CrashLoopBackOff: rollout with corrupt seed data
#   scripts/inject-fault.sh E        worker memory leak (runtime)
#   scripts/inject-fault.sh reset    restore baseline manifests and clear runtime faults
#   scripts/inject-fault.sh status
#
# Never targets anything outside the demo namespace. Aliases: checkout-5xx=A,
# inventory-latency=B, clear=reset.
set -euo pipefail
cd "$(dirname "$0")/.."

NS=sentinelops-demo
TOKEN="${FAULT_ADMIN_TOKEN:-local-dev-fault-token}"
KUBECTL=("$PWD/.cache/bin/kubectl" --kubeconfig "$PWD/.cache/kube/admin.kubeconfig" -n "$NS")
PAYMENT="http://127.0.0.1:18083"
WORKER="http://127.0.0.1:18084"

wait_ready() { # url — demo pods may still be rolling after a reset
  for _ in $(seq 1 30); do
    curl -fsS -m 2 "$1/healthz" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "service at $1 not ready" >&2
  return 1
}

admin() { # method url [json]
  local args=(-fsS --retry 5 --retry-all-errors --retry-delay 2 -X "$1" -H "X-Fault-Token: ${TOKEN}" "$2")
  if [[ $# -ge 3 ]]; then args+=(-H "Content-Type: application/json" -d "$3"); fi
  curl "${args[@]}"
}

case "${1:-}" in
  A|checkout-5xx)
    "${KUBECTL[@]}" set env deployment/checkout PAYMENT_PROVIDER_URL='payments.internal:::' >/dev/null
    echo "A: checkout rolled out with a malformed PAYMENT_PROVIDER_URL -> expect CheckoutHigh5xxRate"
    ;;
  B|inventory-latency)
    "${KUBECTL[@]}" set env deployment/inventory FEATURE_NEW_STOCK_INDEX=on >/dev/null
    echo "B: inventory rolled out with FEATURE_NEW_STOCK_INDEX=on -> expect InventoryHighLatencyP95"
    ;;
  C)
    wait_ready "$PAYMENT"
    admin PUT "$PAYMENT/admin/faults/provider_slowdown" '{"delay_ms": 2500}'
    echo "C: payment provider slowed to 2.5s -> checkout timeouts/retries -> CheckoutHigh5xxRate"
    ;;
  D)
    "${KUBECTL[@]}" set env deployment/inventory INVENTORY_STOCK_SEED=corrupt >/dev/null
    echo "D: inventory rolled out with corrupt seed -> new pods crash-loop -> DeploymentCrashLooping"
    ;;
  E)
    wait_ready "$WORKER"
    admin PUT "$WORKER/admin/faults/memory_leak" '{"delay_ms": 512}'
    echo "E: worker leaking ~2.5 MB/s -> expect WorkerMemoryHigh in ~2-3 min"
    ;;
  reset|clear)
    admin DELETE "$PAYMENT/admin/faults" || true
    admin DELETE "$WORKER/admin/faults" || true
    "${KUBECTL[@]}" apply -f infra/kind/workloads.yaml >/dev/null
    "${KUBECTL[@]}" rollout restart deployment/worker deployment/payment >/dev/null
    for d in checkout inventory payment worker; do
      "${KUBECTL[@]}" rollout status "deployment/$d" --timeout=180s >/dev/null
    done
    echo "baseline restored (manifests re-applied, runtime faults cleared)"
    ;;
  status)
    "${KUBECTL[@]}" get deploy -o custom-columns='NAME:.metadata.name,REVISION:.metadata.annotations.deployment\.kubernetes\.io/revision,READY:.status.readyReplicas,WANT:.spec.replicas'
    echo "payment faults: $(admin GET "$PAYMENT/admin/faults")"
    echo "worker faults:  $(admin GET "$WORKER/admin/faults")"
    ;;
  *)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
