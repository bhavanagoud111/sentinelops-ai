# Runbooks

Human-readable runbooks linked from alert annotations. In Phase 4 the actionable subset
becomes allowlisted, machine-checked runbooks; until then these are recommend-only.

## Checkout high 5xx rate
**Alert:** `CheckoutHigh5xxRate` (critical) — checkout 5xx ratio > 5% for 1 min.
**Demo scenario:** A (`make inject-fault`).

1. Grafana → *Service Overview*: confirm error ratio rise is checkout-only, check whether
   inventory latency/errors rose first (dependency vs local cause).
2. Loki: `{service_name="checkout"} | severity_text="ERROR"` — look for
   `configuration error` / `ConfigError: invalid payment provider url`.
3. Tempo: `{resource.service.name="checkout" && status=error}` — failing span
   `checkout.load_payment_config`; inventory is *not* called on these requests.
4. Correlate with recent config/deploy changes.
5. **Remediation (demo):** roll back the bad configuration (`make clear-fault`).
   Phase 4: allowlisted *rollback deployment* runbook with approval.
6. Verify: error ratio < 1% for 5 min; alert resolves.

## Inventory high latency
**Alert:** `InventoryHighLatencyP95` (high) — inventory p95 > 500 ms for 2 min.
**Demo scenario:** B (`make inject-latency`).

1. Service Overview: inventory p95 up; checkout p95 follows; error ratio may stay flat.
2. Tempo: slow traces show long `inventory.store.query` span with `demo.fault.delay_ms`.
3. Loki: `inventory store slow response` warnings.
4. **Remediation (demo):** remove dependency delay (`make clear-fault`). Real-world
   analogue: fail over / scale the backing store; restarting inventory will not help.
5. Verify p95 < 200 ms for 5 min.

## Telemetry pipeline down
**Alert:** `TelemetryPipelineDown` — Prometheus cannot scrape the collector's app metrics.
Inhibits service alerts (they would be unreliable).

1. `make ps` / `make logs SERVICE=otel-collector`.
2. Validate collector config; restart the collector.
