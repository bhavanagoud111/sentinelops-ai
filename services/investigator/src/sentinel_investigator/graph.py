"""Bounded investigation workflow (LangGraph, ADR-005).

    triage → collect_evidence → root_cause → critic → plan_remediation → report

The orchestrator owns control flow: agents cannot call each other or choose the
next step, every node checks the shared deadline, the collector has a fixed
tool budget, and the graph has a hard recursion limit. Any failure ends in an
explicit ``failed``/``abstained`` outcome — never in a silent action.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sentinel_contracts import Incident

from sentinel_investigator import critic as critic_mod
from sentinel_investigator.models import (
    AgentStep,
    CriticFinding,
    CriticVerdict,
    Evidence,
    EvidenceKind,
    Hypothesis,
    InvestigationOutcome,
    InvestigationResult,
    RemediationProposal,
    Triage,
)
from sentinel_investigator.providers.base import AnalysisInput, ModelProvider
from sentinel_investigator.tools import Toolbox, ToolContext

logger = logging.getLogger(__name__)

EVIDENCE_PLAN: tuple[EvidenceKind, ...] = (
    EvidenceKind.ERROR_RATIO,
    EvidenceKind.LATENCY_P95,
    EvidenceKind.REQUEST_RATE,
    EvidenceKind.ERROR_LOGS,
    EvidenceKind.ERROR_TRACES,
    EvidenceKind.SLOW_TRACES,
    EvidenceKind.DEPENDENCY_CALLS,
    EvidenceKind.PROCESS_MEMORY,
    EvidenceKind.ROLLOUTS,
    EvidenceKind.POD_STATUS,
)


@dataclass
class Limits:
    deadline_s: float = 120.0
    max_collect_calls: int = 40
    max_followup_calls: int = 4
    parallelism: int = 6
    lookback: timedelta = timedelta(minutes=20)
    model_critic: bool = True


@dataclass
class Deps:
    provider: ModelProvider
    toolbox: Toolbox
    catalog: list[dict[str, Any]]
    service_graph: dict[str, list[str]]  # caller -> callees
    criticality: dict[str, str]
    limits: Limits = field(default_factory=Limits)
    clock: Any = None  # callable returning aware datetime; injectable for tests


class State(TypedDict, total=False):
    incident: Incident
    deadline: float
    triage: Triage
    ctx: ToolContext
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    summary: str
    findings: list[CriticFinding]
    proposal: RemediationProposal | None
    top: Hypothesis | None
    steps: list[AgentStep]
    outcome: InvestigationOutcome


class DeadlineExceededError(TimeoutError):
    pass


def _check_deadline(state: State) -> None:
    if time.monotonic() > state["deadline"]:
        raise DeadlineExceededError("investigation deadline exceeded")


def dependents_of(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for caller, callees in graph.items():
        for c in callees:
            out.setdefault(c, []).append(caller)
    return out


def build_graph(deps: Deps) -> Any:
    now = deps.clock or (lambda: datetime.now(UTC))

    def step(
        state: State, node: str, started: datetime, summary: str, **kw: Any
    ) -> list[AgentStep]:
        return [
            *state.get("steps", []),
            AgentStep(node=node, started_at=started, finished_at=now(), summary=summary, **kw),
        ]

    async def triage(state: State) -> State:
        started = now()
        inc = state["incident"]
        callees = deps.service_graph.get(inc.service, [])
        scope = [inc.service, *[c for c in callees if c != inc.service]]
        end = now()
        start = min(inc.first_seen - deps.limits.lookback, end - timedelta(minutes=15))
        start = max(start, end - timedelta(hours=6))
        crit = deps.criticality.get(inc.service, "unknown")
        impact = (
            "high"
            if inc.severity.value == "critical" or crit == "critical"
            else "medium"
            if inc.severity.value == "high"
            else "low"
        )
        t = Triage(
            impact=impact,
            service=inc.service,
            criticality=crit,
            scope=scope,
            window_start=start,
            window_end=end,
            notes=f"{inc.title}; dependencies examined: {callees or 'none'}",
        )
        ctx = ToolContext(incident_id=inc.id, scope=frozenset(scope), start=start, end=end)
        return {
            "triage": t,
            "ctx": ctx,
            "steps": step(
                state,
                "triage",
                started,
                f"impact={impact}, criticality={crit}, scope={scope}, "
                f"window={start:%H:%M}-{end:%H:%M} UTC",
            ),
        }

    async def collect(state: State) -> State:
        _check_deadline(state)
        started = now()
        ctx = state["ctx"]
        jobs = [(k, s) for s in state["triage"].scope for k in EVIDENCE_PLAN]
        jobs = jobs[: deps.limits.max_collect_calls]
        sem = asyncio.Semaphore(deps.limits.parallelism)

        async def one(kind: EvidenceKind, svc: str) -> Evidence:
            async with sem:
                return await deps.toolbox.collect(kind, svc, ctx)

        evidence = list(await asyncio.gather(*(one(k, s) for k, s in jobs)))
        anomalous = [e for e in evidence if e.anomalous]
        calls = [
            {
                "tool": e.tool,
                "service": e.service,
                "kind": e.kind.value,
                "evidence_id": e.evidence_id,
                "anomalous": e.anomalous,
                "error": e.error,
            }
            for e in evidence
        ]
        return {
            "evidence": evidence,
            "steps": step(
                state,
                "collect_evidence",
                started,
                f"{len(evidence)} read-only queries, {len(anomalous)} anomalous: "
                + "; ".join(e.summary for e in anomalous[:4]),
                tool_calls=calls,
            ),
        }

    async def root_cause(state: State) -> State:
        _check_deadline(state)
        started = now()
        data = AnalysisInput(
            incident=state["incident"],
            triage=state["triage"],
            evidence=state["evidence"],
            catalog=deps.catalog,
            service_graph=deps.service_graph,
        )
        result = await deps.provider.hypothesize(
            data, deps.toolbox, state["ctx"], deps.limits.max_followup_calls
        )
        hyps = result.output.hypotheses
        return {
            "evidence": [*state["evidence"], *result.extra_evidence],
            "hypotheses": hyps,
            "summary": result.summary,
            "steps": step(
                state,
                "root_cause",
                started,
                f"{len(hyps)} hypotheses: "
                + "; ".join(
                    f"{h.hypothesis_id} {h.cause_category.value}@"
                    f"{h.suspected_service} ({h.confidence:.2f})"
                    for h in hyps
                ),
                tool_calls=result.tool_calls,
                model=result.model or deps.provider.name,
                usage=result.usage,
            ),
        }

    async def critic(state: State) -> State:
        _check_deadline(state)
        started = now()
        findings = critic_mod.review(state["hypotheses"], state["evidence"], state["triage"].scope)
        usage: dict[str, int] = {}
        if deps.limits.model_critic:
            data = AnalysisInput(
                state["incident"],
                state["triage"],
                state["evidence"],
                deps.catalog,
                deps.service_graph,
            )
            from sentinel_investigator.models import HypothesesOutput

            model_findings, usage = await deps.provider.critique(
                data, HypothesesOutput(hypotheses=state["hypotheses"])
            )
            findings = critic_mod.merge_model_findings(findings, model_findings)
        verdicts = ", ".join(
            f"{f.hypothesis_id}:{f.verdict.value}({f.adjusted_confidence:.2f})" for f in findings
        )
        return {
            "findings": findings,
            "steps": step(state, "critic", started, verdicts, usage=usage),
        }

    async def plan_remediation(state: State) -> State:
        started = now()
        proposal, top = critic_mod.plan(
            state["hypotheses"],
            state["findings"],
            deps.criticality,
            dependents_of(deps.service_graph),
        )
        if proposal:
            outcome = InvestigationOutcome.PROPOSED
            msg = f"proposed {proposal.runbook_id} {proposal.params} (risk {proposal.risk})"
        elif top:
            outcome = InvestigationOutcome.ESCALATED
            msg = (
                f"no safe allowlisted action for {top.cause_category.value}@"
                f"{top.suspected_service}; escalating to a human"
            )
        else:
            outcome = InvestigationOutcome.ABSTAINED
            msg = "no hypothesis survived verification; abstaining"
        return {
            "proposal": proposal,
            "top": top,
            "outcome": outcome,
            "steps": step(state, "plan_remediation", started, msg),
        }

    async def report(state: State) -> State:
        started = now()
        findings = {f.hypothesis_id: f for f in state.get("findings", [])}
        top = state.get("top")
        lines = [state.get("summary", "").strip()]
        if top:
            f = findings[top.hypothesis_id]
            lines.append(
                f"Verified hypothesis {top.hypothesis_id}: {top.cause_category.value} in "
                f"{top.suspected_service} (confidence {f.adjusted_confidence:.2f}, "
                f"{f.verdict.value}). Evidence: {', '.join(top.evidence_ids)}."
            )
        rejected = [f for f in findings.values() if f.verdict is CriticVerdict.REJECTED]
        if rejected:
            lines.append(f"{len(rejected)} hypothesis(es) rejected by the critic.")
        p = state.get("proposal")
        lines.append(
            f"Proposed remediation: {p.runbook_id} {p.params} — requires human approval."
            if p
            else "No remediation proposed; human follow-up required."
        )
        return {
            "summary": "\n".join(x for x in lines if x),
            "steps": step(state, "report", started, "summary written"),
        }

    g = StateGraph(State)
    g.add_node("triage", triage)
    g.add_node("collect_evidence", collect)
    g.add_node("root_cause", root_cause)
    g.add_node("critic", critic)
    g.add_node("plan_remediation", plan_remediation)
    g.add_node("report", report)
    g.add_edge(START, "triage")
    g.add_edge("triage", "collect_evidence")
    g.add_edge("collect_evidence", "root_cause")
    g.add_edge("root_cause", "critic")
    g.add_edge("critic", "plan_remediation")
    g.add_edge("plan_remediation", "report")
    g.add_edge("report", END)
    return g.compile()


async def run_investigation(incident: Incident, deps: Deps) -> InvestigationResult:
    graph = build_graph(deps)
    state: State = {
        "incident": incident,
        "deadline": time.monotonic() + deps.limits.deadline_s,
        "steps": [],
    }
    try:
        final: State = await asyncio.wait_for(
            graph.ainvoke(state, {"recursion_limit": 12}),
            timeout=deps.limits.deadline_s,
        )
    except Exception as exc:  # deadline, provider failure, backend outage
        logger.exception("investigation failed", extra={"incident_id": str(incident.id)})
        return InvestigationResult(
            outcome=InvestigationOutcome.FAILED,
            summary=f"Investigation failed: {type(exc).__name__}: {str(exc)[:300]}",
            triage=None,
            evidence=[],
            hypotheses=[],
            findings=[],
            proposal=None,
            steps=[],
            provider=deps.provider.name,
            error=f"{type(exc).__name__}: {exc}"[:1000],
        )
    return InvestigationResult(
        outcome=final["outcome"],
        summary=final.get("summary", ""),
        triage=final.get("triage"),
        evidence=final.get("evidence", []),
        hypotheses=final.get("hypotheses", []),
        findings=final.get("findings", []),
        proposal=final.get("proposal"),
        steps=final.get("steps", []),
        provider=deps.provider.name,
    )
