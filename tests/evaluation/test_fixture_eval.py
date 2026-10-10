"""Phase 3 exit gate (mock mode): the deterministic provider passes every scenario fixture,
cites only real evidence, abstains when evidence is missing, and is unaffected by
prompt-injection text in logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sentinel_investigator.evaluation import evaluate
from sentinel_investigator.providers.rules import RulesProvider

FIXTURES = sorted(
    (Path(__file__).resolve().parents[2] / "demo" / "datasets" / "scenarios").glob("*.json")
)


def test_fixture_set_is_complete() -> None:
    names = {p.stem.split("_")[0] for p in FIXTURES}
    assert {"A", "B", "C", "D", "E", "N1", "N2"} <= names


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
async def test_rules_provider_scenario(path: Path) -> None:
    score, result = await evaluate(path, RulesProvider())
    assert score.citations_valid, "every cited evidence id must exist"
    assert not score.unauthorized_action
    assert score.outcome_ok, f"outcome {score.outcome}; summary: {result.summary}"
    assert score.top1_ok, f"top hypothesis {score.top}"
    assert score.runbook_ok, f"proposal {result.proposal}"
    # Agent activity log covers every node.
    assert [s.node for s in result.steps] == [
        "triage",
        "collect_evidence",
        "root_cause",
        "critic",
        "plan_remediation",
        "report",
    ]
