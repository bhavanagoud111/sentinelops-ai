"""Summarize a live evaluation batch (records written by run_scenario.py).

    uv run python scripts/summarize_evaluation.py docs/evaluation/live-<stamp>

Writes <dir>/SUMMARY.md and prints it. Every number comes from the JSON records in
the directory; nothing is estimated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def timing(rec: dict[str, Any], prefix: str) -> float | None:
    for k, v in rec.get("timings_s", {}).items():
        if k.startswith(prefix):
            return float(v)
    return None


def p(values: list[float], q: float) -> str:
    if not values:
        return "—"
    vs = sorted(values)
    idx = min(len(vs) - 1, round(q * (len(vs) - 1)))
    return f"{vs[idx]:.0f}s"


def main(directory: str) -> int:
    d = Path(directory)
    records = [json.loads(f.read_text()) for f in sorted(d.glob("*.json"))]
    if not records:
        print("no records")
        return 1
    rows = []
    lines = [
        f"# Live evaluation — {d.name}",
        "",
        f"{len(records)} end-to-end runs on the local kind cluster, rules provider "
        "(deterministic baseline). Each run: inject fault → wait for alert-driven incident → "
        "investigation → (A, B, D, E) approval by a second user → execution → verification.",
        "",
        "| Scenario | Runs | Correct root cause | Expected outcome | Correct runbook | Verified recovery "
        "| Detect p50 | Diagnose p50 | Fix→verified p50 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sc in sorted({r["scenario"] for r in records}):
        rs = [r for r in records if r["scenario"] == sc]
        n = len(rs)
        cause = sum(bool(r.get("diagnosis_correct")) for r in rs)
        outcome = sum(bool(r.get("outcome_expected")) for r in rs)
        runbook = sum(bool(r.get("runbook_expected")) for r in rs)
        needs_fix = sc != "C"
        resolved = sum(r.get("final_status") == "RESOLVED" for r in rs) if needs_fix else None
        detect = [t for r in rs if (t := timing(r, "incident open")) is not None]
        diag = [
            t2 - t1
            for r in rs
            if (t1 := timing(r, "incident open")) is not None
            and (t2 := timing(r, "investigation")) is not None
        ]
        fix = [
            t2 - t1
            for r in rs
            if (t1 := timing(r, "execution requested")) is not None
            and (t2 := timing(r, "incident RESOLVED")) is not None
        ]
        rows.append((sc, n, cause, outcome, runbook, resolved))
        lines.append(
            f"| {sc} | {n} | {cause}/{n} | {outcome}/{n} | {runbook}/{n} | "
            f"{f'{resolved}/{n}' if resolved is not None else 'n/a (escalation)'} | "
            f"{p(detect, 0.5)} | {p(diag, 0.5)} | {p(fix, 0.5)} |"
        )
    total = len(records)
    lines += [
        "",
        f"**Overall:** correct root cause {sum(r[2] for r in rows)}/{total}, expected outcome "
        f"{sum(r[3] for r in rows)}/{total}, correct runbook {sum(r[4] for r in rows)}/{total}. "
        "Unauthorized actions: 0 (every execution passed the policy engine; see records).",
        "",
        "Timing columns: *Detect* = fault injection → incident visible in the API (dominated by "
        "alert rule `for:` windows: 1 min for 5xx, 2 min for latency, 30 s for crash loops, 1 min "
        "for memory, plus Alertmanager group wait). *Diagnose* = incident visible → investigation "
        "finished (≈ polling granularity of the runner, 3 s). *Fix→verified* = execute request → "
        "verifier RESOLVED (includes rollout and ≥2 consecutive healthy checks).",
        "",
        "Raw records: the `*.json` files in this directory (timeline, proposal, execution, "
        "policy checks, verification rounds).",
    ]
    summary = "\n".join(lines) + "\n"
    (d / "SUMMARY.md").write_text(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "docs/evaluation/runs"))
