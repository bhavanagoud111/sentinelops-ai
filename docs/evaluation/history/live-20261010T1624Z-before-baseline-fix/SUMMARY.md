# Live evaluation — live-20261010T1624Z

15 end-to-end runs on the local kind cluster, rules provider (deterministic baseline). Each run: inject fault → wait for alert-driven incident → investigation → (A, B, D, E) approval by a second user → execution → verification.

| Scenario | Runs | Correct root cause | Expected outcome | Correct runbook | Verified recovery | Detect p50 | Diagnose p50 | Fix→verified p50 |
|---|---|---|---|---|---|---|---|---|
| A | 3 | 2/3 | 2/3 | 2/3 | 2/3 | 112s | 0s | 57s |
| B | 3 | 1/3 | 1/3 | 1/3 | 1/3 | 166s | 0s | 57s |
| C | 3 | 2/3 | 3/3 | 3/3 | n/a (escalation) | 103s | 0s | — |
| D | 3 | 3/3 | 3/3 | 3/3 | 3/3 | 63s | 0s | 54s |
| E | 3 | 3/3 | 3/3 | 3/3 | 3/3 | 180s | 0s | 133s |

**Overall:** correct root cause 11/15, expected outcome 12/15, correct runbook 12/15. Unauthorized actions: 0 (every execution passed the policy engine; see records).

Timing columns: *Detect* = fault injection → incident visible in the API (dominated by alert rule `for:` windows: 1 min for 5xx, 2 min for latency, 30 s for crash loops, 1 min for memory, plus Alertmanager group wait). *Diagnose* = incident visible → investigation finished (≈ polling granularity of the runner, 3 s). *Fix→verified* = execute request → verifier RESOLVED (includes rollout and ≥2 consecutive healthy checks).

Raw records: the `*.json` files in this directory (timeline, proposal, execution, policy checks, verification rounds).
