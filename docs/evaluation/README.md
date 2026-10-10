# Evaluation records

| Path | What it is |
|---|---|
| [`live-20261010T1716Z/`](live-20261010T1716Z/SUMMARY.md) | **Published result.** 5 scenarios × 3 end-to-end runs on the local kind cluster: 15/15 correct root cause, 0 unauthorized actions. One JSON record per run plus `SUMMARY.md`. |
| [`history/`](history) | Earlier live batches, kept unchanged. They exposed four investigator bugs (fixed with regression tests); also the runs replaced in the published batch, with the reason in its run notes. The `1532Z` batch was interrupted and has no summary. |
| [`dev-runs/`](dev-runs/README.md) | Single runs recorded while the remediation phase was being built. |
| `fixtures-*.json` | Offline replay of recorded scenario fixtures (`scripts/evaluate.py`), no cluster needed. The same fixtures run in CI via `make check`. |

Reproduce: `make up`, then `scripts/run_evaluation.sh 3` (about an hour; writes a new
`live-<timestamp>/` folder) or a single scenario with
`uv run python scripts/run_scenario.py A --approve`.
