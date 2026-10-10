# Live evaluation — live-20261010T1716Z

15 end-to-end runs on the local kind cluster, rules provider (deterministic baseline). Each run: inject fault → wait for alert-driven incident → investigation → (A, B, D, E) approval by a second user → execution → verification.

| Scenario | Runs | Correct root cause | Expected outcome | Correct runbook | Verified recovery | Detect p50 | Diagnose p50 | Fix→verified p50 |
|---|---|---|---|---|---|---|---|---|
| A | 3 | 3/3 | 3/3 | 3/3 | 3/3 | 106s | 0s | 96s |
| B | 3 | 3/3 | 3/3 | 3/3 | 3/3 | 166s | 0s | 60s |
| C | 3 | 3/3 | 3/3 | 3/3 | n/a (escalation) | 105s | 0s | — |
| D | 3 | 3/3 | 3/3 | 3/3 | 3/3 | 63s | 0s | 54s |
| E | 3 | 3/3 | 3/3 | 3/3 | 3/3 | 189s | 0s | 139s |

**Overall:** correct root cause 15/15, expected outcome 15/15, correct runbook 15/15. Unauthorized actions: 0 (every execution passed the policy engine; see records).

Timing columns: *Detect* = fault injection → incident visible in the API (dominated by alert rule `for:` windows: 1 min for 5xx, 2 min for latency, 30 s for crash loops, 1 min for memory, plus Alertmanager group wait). *Diagnose* = incident visible → investigation finished (≈ polling granularity of the runner, 3 s). *Fix→verified* = execute request → verifier RESOLVED (includes rollout and ≥2 consecutive healthy checks).

Raw records: the `*.json` files in this directory (timeline, proposal, execution, policy checks, verification rounds).

## Run notes
Four runs in this batch were replaced, all for test-harness reasons, never to improve a score.
The originals are kept in [`../history/`](../history):

- **Run 1 A and run 1 C were lost.** The batch started seconds after the integration tests had made
  `CheckoutHigh5xxRate` fire, so the alert never cleared. Alertmanager does not re-send a
  continuously firing alert (`repeat_interval: 1h`), so no new incident opened and the runner
  timed out. The runner also exited without restoring the baseline.
- **Run 2 A was contaminated** by that leftover fault: checkout had been failing 100% for the whole
  20-minute look-back, so there was no healthy "before" and the investigation correctly abstained.
  Record: `history/live-20261010T1716Z-harness-contaminated/`.
- **One replacement A ran within 5 minutes of the previous rollback.** The policy engine blocked
  the approved rollback (`cooldown_elapsed`), as designed. Record:
  `history/live-20261010T1716Z-policy-cooldown/`. It was re-run after the cooldown.

The runner now waits for the scenario's alert to clear before injecting and always restores the
baseline, even on failure. Earlier batches in `history/` led to four investigator fixes
(ReplicaSet reuse, memory aggregation, earlier-episode baselines, per-dependency recency);
their records are kept as-is.
