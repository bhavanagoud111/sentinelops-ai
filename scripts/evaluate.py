"""Run the fixture evaluation and write a results file.

    uv run python scripts/evaluate.py                       # rules baseline only (free)
    uv run python scripts/evaluate.py --providers rules,claude --runs 3

Claude runs cost money and need credentials (ANTHROPIC_API_KEY or `ant auth login`);
they are never run by default. Results go to docs/evaluation/fixtures-<timestamp>.json
and a markdown table on stdout. Numbers are only ever produced by running this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel_investigator.evaluation import evaluate
from sentinel_investigator.providers.base import ModelProvider

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "demo" / "datasets" / "scenarios").glob("*.json"))


def make(name: str) -> ModelProvider:
    if name == "rules":
        from sentinel_investigator.providers.rules import RulesProvider

        return RulesProvider()
    if name == "claude":
        from sentinel_investigator.providers.claude import ClaudeProvider

        return ClaudeProvider()
    raise SystemExit(f"unknown provider {name}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="rules")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--model-critic", action="store_true")
    args = ap.parse_args()

    rows: list[dict[str, Any]] = []
    for pname in args.providers.split(","):
        provider = make(pname)
        for path in FIXTURES:
            for run in range(args.runs):
                t0 = time.perf_counter()
                score, result = await evaluate(path, provider, model_critic=args.model_critic)
                usage: dict[str, int] = {}
                for s in result.steps:
                    for k, v in s.usage.items():
                        usage[k] = usage.get(k, 0) + v
                rows.append(
                    {
                        "provider": pname,
                        "scenario": path.stem,
                        "run": run,
                        "passed": score.passed,
                        "top1": score.top1_ok,
                        "top3": score.top3_ok,
                        "outcome_ok": score.outcome_ok,
                        "runbook_ok": score.runbook_ok,
                        "citations_valid": score.citations_valid,
                        "unauthorized_action": score.unauthorized_action,
                        "outcome": score.outcome,
                        "top": score.top,
                        "seconds": round(time.perf_counter() - t0, 3),
                        "usage": usage,
                        "error": result.error,
                    }
                )

    print(
        "| provider | scenario | runs | pass | top-1 | top-3 | citations ok "
        "| unauthorized | p50 s |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    keys = sorted({(r["provider"], r["scenario"]) for r in rows})
    for prov, sc in keys:
        rs = [r for r in rows if r["provider"] == prov and r["scenario"] == sc]
        print(
            f"| {prov} | {sc} | {len(rs)} | {sum(r['passed'] for r in rs)}/{len(rs)} "
            f"| {sum(r['top1'] for r in rs)}/{len(rs)} | {sum(r['top3'] for r in rs)}/{len(rs)} "
            f"| {sum(r['citations_valid'] for r in rs)}/{len(rs)} "
            f"| {sum(r['unauthorized_action'] for r in rs)} "
            f"| {statistics.median(r['seconds'] for r in rs):.3f} |"
        )
    out = ROOT / "docs" / "evaluation" / f"fixtures-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(UTC).isoformat(), "rows": rows}
    out.write_text(json.dumps(payload, indent=1))
    print(f"\nraw results: {out.relative_to(ROOT)}")
    return 0 if all(r["passed"] for r in rows if r["provider"] == "rules") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
