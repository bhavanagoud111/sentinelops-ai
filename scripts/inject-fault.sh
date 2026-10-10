#!/usr/bin/env bash
# Inject or clear a demo fault in the sandbox workload.
#
#   scripts/inject-fault.sh checkout-5xx [probability]      # scenario A (default 0.8)
#   scripts/inject-fault.sh inventory-latency [delay_ms]    # scenario B (default 1200)
#   scripts/inject-fault.sh clear                           # clear all faults
#   scripts/inject-fault.sh status
#
# Only targets the local demo services on 127.0.0.1.
set -euo pipefail

TOKEN="${FAULT_ADMIN_TOKEN:-local-dev-fault-token}"
CHECKOUT="http://127.0.0.1:${CHECKOUT_PORT:-18081}"
INVENTORY="http://127.0.0.1:${INVENTORY_PORT:-18082}"

call() { # method url [json]
  local args=(-fsS -X "$1" -H "X-Fault-Token: ${TOKEN}" "$2")
  if [[ $# -ge 3 ]]; then args+=(-H "Content-Type: application/json" -d "$3"); fi
  curl "${args[@]}"
}

case "${1:-}" in
  checkout-5xx)
    p="${2:-0.8}"
    call PUT "${CHECKOUT}/admin/faults/bad_config" "{\"probability\": ${p}}"
    echo "scenario A: checkout bad_config active (probability=${p}); expect CheckoutHigh5xxRate in ~2 min"
    ;;
  inventory-latency)
    d="${2:-1200}"
    call PUT "${INVENTORY}/admin/faults/dependency_delay" "{\"delay_ms\": ${d}}"
    echo "scenario B: inventory dependency_delay active (delay_ms=${d}); expect InventoryHighLatencyP95 in ~3 min"
    ;;
  clear)
    call DELETE "${CHECKOUT}/admin/faults"
    call DELETE "${INVENTORY}/admin/faults"
    echo "all demo faults cleared"
    ;;
  status)
    echo "checkout:  $(call GET "${CHECKOUT}/admin/faults")"
    echo "inventory: $(call GET "${INVENTORY}/admin/faults")"
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
