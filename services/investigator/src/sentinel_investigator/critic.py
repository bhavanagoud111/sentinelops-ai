"""Deterministic critic/verifier and remediation planner.

The critic is the anti-hallucination gate. It does not trust the provider:
* every cited evidence id must exist in this investigation (else: fabricated → reject)
* at least one cited evidence item must be anomalous (else: unsupported → reject)
* the category must be corroborated by the right *kind* of evidence (else: downgrade)
* a recommended runbook must be allowlisted, parse against its parameter model,
  and target the suspected service in the demo namespace (else: runbook stripped)

The planner then proposes at most one remediation, only from an accepted
hypothesis above the confidence threshold. Otherwise the investigation escalates
or abstains — it never guesses an action.
"""

from __future__ import annotations

from pydantic import ValidationError
from sentinel_policy import CATALOG, DEMO_NAMESPACE, RunbookId, parse_params

from sentinel_investigator.models import (
    CauseCategory,
    CriticFinding,
    CriticVerdict,
    Evidence,
    EvidenceKind,
    Hypothesis,
    RemediationProposal,
)

K = EvidenceKind
PROPOSAL_THRESHOLD = 0.6
REJECT_BELOW = 0.25

CORROBORATION: dict[CauseCategory, set[EvidenceKind]] = {
    CauseCategory.BAD_CONFIG_ROLLOUT: {K.ROLLOUTS, K.ERROR_LOGS},
    CauseCategory.CRASH_LOOP_ROLLOUT: {K.POD_STATUS},
    CauseCategory.FEATURE_FLAG_REGRESSION: {K.ROLLOUTS},
    CauseCategory.DEPENDENCY_FAILURE: {K.DEPENDENCY_CALLS},
    CauseCategory.RESOURCE_SATURATION: {K.PROCESS_MEMORY, K.POD_STATUS},
    CauseCategory.UNKNOWN: set(),
}


def review(
    hypotheses: list[Hypothesis], evidence: list[Evidence], scope: list[str]
) -> list[CriticFinding]:
    by_id = {e.evidence_id: e for e in evidence}
    findings: list[CriticFinding] = []
    for h in hypotheses:
        reasons: list[str] = []
        conf = h.confidence
        verdict: CriticVerdict = CriticVerdict.ACCEPTED

        unknown = [i for i in h.evidence_ids + h.counterevidence_ids if i not in by_id]
        if unknown:
            findings.append(
                CriticFinding(
                    hypothesis_id=h.hypothesis_id,
                    verdict=CriticVerdict.REJECTED,
                    adjusted_confidence=0.0,
                    reasons=[f"cites evidence that does not exist: {unknown[:5]}"],
                )
            )
            continue
        if h.suspected_service not in scope:
            findings.append(
                CriticFinding(
                    hypothesis_id=h.hypothesis_id,
                    verdict=CriticVerdict.REJECTED,
                    adjusted_confidence=0.0,
                    reasons=[f"suspected service {h.suspected_service!r} outside scope {scope}"],
                )
            )
            continue
        cited = [by_id[i] for i in h.evidence_ids]
        if not any(e.anomalous for e in cited):
            verdict = CriticVerdict.REJECTED
            conf = 0.0
            reasons.append("no cited evidence shows an anomaly")
        else:
            needed = CORROBORATION.get(h.cause_category, set())
            if needed and not any(e.kind in needed and e.anomalous for e in cited):
                conf = round(conf * 0.5, 2)
                verdict = CriticVerdict.DOWNGRADED
                reasons.append(
                    f"{h.cause_category.value} not corroborated by "
                    f"{sorted(k.value for k in needed)} evidence"
                )
            counter = [by_id[i] for i in h.counterevidence_ids]
            if counter:
                conf = round(conf * 0.8, 2)
                verdict = CriticVerdict.DOWNGRADED
                reasons.append(f"{len(counter)} counter-evidence item(s) cited")
        if h.cause_category is CauseCategory.UNKNOWN:
            conf = min(conf, REJECT_BELOW)
            reasons.append("unknown cause: abstain")
        if verdict is not CriticVerdict.REJECTED and conf < REJECT_BELOW:
            verdict = CriticVerdict.REJECTED
            reasons.append(f"confidence {conf} below {REJECT_BELOW}")

        rb_problem = runbook_problem(h)
        if rb_problem:
            reasons.append(f"runbook removed: {rb_problem}")
        findings.append(
            CriticFinding(
                hypothesis_id=h.hypothesis_id,
                verdict=verdict,
                adjusted_confidence=conf,
                reasons=reasons or ["supported by cited evidence"],
            )
        )
    return findings


def runbook_problem(h: Hypothesis) -> str | None:
    if h.recommended_runbook_id is None:
        return None
    try:
        rid = RunbookId(h.recommended_runbook_id)
    except ValueError:
        return f"{h.recommended_runbook_id!r} is not an allowlisted runbook"
    try:
        params = parse_params(rid.value, dict(h.runbook_params))
    except ValidationError as exc:
        return f"invalid parameters ({exc.error_count()} errors)"
    rb = CATALOG[rid]
    if params.namespace != DEMO_NAMESPACE or params.namespace not in rb.allowed_namespaces:
        return f"namespace {params.namespace!r} not allowed"
    if params.deployment not in rb.allowed_deployments:
        return f"deployment {params.deployment!r} not allowed"
    if params.deployment != h.suspected_service:
        return "runbook targets a different service than the suspected cause"
    return None


def merge_model_findings(
    base: list[CriticFinding], model: list[CriticFinding]
) -> list[CriticFinding]:
    """Apply a model critic's opinion, which can only lower confidence or reject."""
    by_id = {f.hypothesis_id: f for f in model}
    merged: list[CriticFinding] = []
    for f in base:
        m = by_id.get(f.hypothesis_id)
        if m is None or f.verdict is CriticVerdict.REJECTED:
            merged.append(f)
            continue
        conf = min(f.adjusted_confidence, m.adjusted_confidence)
        verdict: CriticVerdict = f.verdict
        if m.verdict is CriticVerdict.REJECTED:
            verdict = CriticVerdict.REJECTED
        elif conf < f.adjusted_confidence:
            verdict = CriticVerdict.DOWNGRADED
        merged.append(
            CriticFinding(
                hypothesis_id=f.hypothesis_id,
                verdict=verdict,
                adjusted_confidence=conf,
                reasons=f.reasons + [f"model critic: {r}" for r in m.reasons],
            )
        )
    return merged


def plan(
    hypotheses: list[Hypothesis],
    findings: list[CriticFinding],
    service_criticality: dict[str, str],
    dependents: dict[str, list[str]],
) -> tuple[RemediationProposal | None, Hypothesis | None]:
    """Return (proposal, top accepted hypothesis)."""
    by_id = {f.hypothesis_id: f for f in findings}
    ranked = sorted(
        (h for h in hypotheses if by_id[h.hypothesis_id].verdict is not CriticVerdict.REJECTED),
        key=lambda h: by_id[h.hypothesis_id].adjusted_confidence,
        reverse=True,
    )
    if not ranked:
        return None, None
    top = ranked[0]
    conf = by_id[top.hypothesis_id].adjusted_confidence
    if (
        conf < PROPOSAL_THRESHOLD
        or top.recommended_runbook_id is None
        or runbook_problem(top) is not None
    ):
        return None, top
    rb = CATALOG[RunbookId(top.recommended_runbook_id)]
    params = parse_params(rb.id.value, dict(top.runbook_params))
    return (
        RemediationProposal(
            runbook_id=rb.id.value,
            params=params.model_dump(exclude={"action"}),
            risk=rb.risk.value,
            hypothesis_id=top.hypothesis_id,
            rationale=top.explanation,
            evidence_ids=top.evidence_ids,
            blast_radius={
                "namespace": params.namespace,
                "deployment": params.deployment,
                "criticality": service_criticality.get(params.deployment, "unknown"),
                "dependents": dependents.get(params.deployment, []),
                "risk": rb.risk.value,
            },
        ),
        top,
    )
