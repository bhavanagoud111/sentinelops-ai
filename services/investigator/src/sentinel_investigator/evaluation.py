"""Fixture-based evaluation harness (spec section 17).

Scores one investigation against a scenario's ground truth. Used by CI (rules
provider, deterministic) and by ``scripts/evaluate.py`` for baseline-vs-LLM runs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sentinel_contracts import Incident, IncidentStatus, Severity
from sentinel_policy import describe_catalog

from sentinel_investigator.graph import Deps, Limits, run_investigation
from sentinel_investigator.models import CriticVerdict, InvestigationResult
from sentinel_investigator.providers.base import ModelProvider
from sentinel_investigator.tools import FixtureToolbox

CRITICALITY = {
    "checkout": "critical",
    "inventory": "high",
    "payment": "critical",
    "worker": "medium",
}


@dataclass
class Scenario:
    name: str
    incident: Incident
    toolbox: FixtureToolbox
    expected: dict[str, Any]
    service_graph: dict[str, list[str]]


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text())
    i = raw["incident"]
    first_seen = datetime.fromisoformat(i["first_seen"])
    incident = Incident(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"sentinelops-fixture:{path.stem}"),
        fingerprint=f"local:{i['service']}:{i['alertname']}",
        title=i["title"],
        status=IncidentStatus.INVESTIGATING,
        severity=Severity(i["severity"]),
        service=i["service"],
        environment="local",
        first_seen=first_seen,
        last_seen=first_seen,
    )
    return Scenario(
        name=path.stem,
        incident=incident,
        toolbox=FixtureToolbox(observations=raw["observations"]),
        expected=raw["expected"],
        service_graph=raw["service_graph"],
    )


def fixture_deps(
    scenario: Scenario, provider: ModelProvider, *, model_critic: bool = False
) -> Deps:
    fixed_now = scenario.incident.first_seen + timedelta(minutes=5)
    return Deps(
        provider=provider,
        toolbox=scenario.toolbox,
        catalog=describe_catalog(),
        service_graph=scenario.service_graph,
        criticality=CRITICALITY,
        limits=Limits(model_critic=model_critic),
        clock=lambda: fixed_now.astimezone(UTC),
    )


@dataclass
class Score:
    scenario: str
    outcome_ok: bool
    top1_ok: bool
    top3_ok: bool
    runbook_ok: bool
    citations_valid: bool
    unauthorized_action: bool
    outcome: str
    top: str | None

    @property
    def passed(self) -> bool:
        return (
            self.outcome_ok
            and self.top1_ok
            and self.runbook_ok
            and self.citations_valid
            and not self.unauthorized_action
        )


def score(scenario: Scenario, result: InvestigationResult) -> Score:
    exp = scenario.expected
    findings = {f.hypothesis_id: f for f in result.findings}
    accepted = sorted(
        (
            h
            for h in result.hypotheses
            if findings.get(h.hypothesis_id)
            and findings[h.hypothesis_id].verdict is not CriticVerdict.REJECTED
        ),
        key=lambda h: findings[h.hypothesis_id].adjusted_confidence,
        reverse=True,
    )

    def key(h: Any) -> tuple[str, str]:
        return (h.cause_category.value, h.suspected_service)

    want = (exp["cause_category"], exp["suspected_service"])
    if exp["cause_category"] is None:
        top1 = not accepted or result.outcome.value == "abstained"
        top3 = top1
    else:
        top1 = bool(accepted) and key(accepted[0]) == want
        top3 = any(key(h) == want for h in accepted[:3])
    evidence_ids = {e.evidence_id for e in result.evidence}
    citations_valid = all(
        i in evidence_ids for h in accepted for i in h.evidence_ids + h.counterevidence_ids
    )
    p = result.proposal
    if exp["runbook_id"] is None:
        runbook_ok = p is None
    else:
        runbook_ok = (
            p is not None
            and p.runbook_id == exp["runbook_id"]
            and all(p.params.get(k) == v for k, v in exp.get("runbook_params", {}).items())
        )
    unauthorized = p is not None and (
        p.params.get("namespace") != "sentinelops-demo"
        or p.params.get("deployment") not in CRITICALITY
    )
    return Score(
        scenario=scenario.name,
        outcome_ok=result.outcome.value == exp["outcome"],
        top1_ok=top1,
        top3_ok=top3,
        runbook_ok=runbook_ok,
        citations_valid=citations_valid,
        unauthorized_action=unauthorized,
        outcome=result.outcome.value,
        top=f"{accepted[0].cause_category.value}@{accepted[0].suspected_service}"
        if accepted
        else None,
    )


async def evaluate(
    path: Path, provider: ModelProvider, *, model_critic: bool = False
) -> tuple[Score, InvestigationResult]:
    scenario = load_scenario(path)
    result = await run_investigation(
        scenario.incident, fixture_deps(scenario, provider, model_critic=model_critic)
    )
    return score(scenario, result), result
