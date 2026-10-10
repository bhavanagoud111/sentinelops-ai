"""Investigation data model: evidence, hypotheses, proposals, report.

Evidence is immutable and content-addressed: ``evidence_id`` is derived from the
incident, source, query, time range and a hash of the bounded result, so the same
observation always has the same id and a cited id can be checked for existence.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


def sanitize(value: Any) -> Any:
    """Make signals JSON/Postgres-safe: non-finite floats become None, keys become str."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


class EvidenceKind(StrEnum):
    ERROR_RATIO = "metric.error_ratio"
    LATENCY_P95 = "metric.latency_p95"
    REQUEST_RATE = "metric.request_rate"
    DEPENDENCY_CALLS = "metric.dependency_calls"
    PROCESS_MEMORY = "metric.process_memory"
    ERROR_LOGS = "logs.errors"
    ERROR_TRACES = "traces.errors"
    SLOW_TRACES = "traces.slow"
    ROLLOUTS = "k8s.rollouts"
    POD_STATUS = "k8s.pod_status"


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    incident_id: uuid.UUID
    kind: EvidenceKind
    service: str
    source: str  # prometheus | loki | tempo | kubernetes
    tool: str
    query: str
    start: AwareDatetime
    end: AwareDatetime
    retrieved_at: AwareDatetime
    summary: str = Field(max_length=1000)
    anomalous: bool
    signals: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    error: str | None = None

    @staticmethod
    def make(
        *,
        incident_id: uuid.UUID,
        kind: EvidenceKind,
        service: str,
        source: str,
        tool: str,
        query: str,
        start: datetime,
        end: datetime,
        retrieved_at: datetime,
        summary: str,
        anomalous: bool,
        signals: dict[str, Any],
        error: str | None = None,
    ) -> Evidence:
        signals = sanitize(signals)
        body = json.dumps(signals, sort_keys=True, default=str, separators=(",", ":"))
        content_hash = hashlib.sha256(body.encode()).hexdigest()
        ident = hashlib.sha256(
            "|".join(
                [
                    str(incident_id),
                    source,
                    tool,
                    query,
                    start.isoformat(),
                    end.isoformat(),
                    content_hash,
                ]
            ).encode()
        ).hexdigest()[:16]
        return Evidence(
            evidence_id=f"ev_{ident}",
            incident_id=incident_id,
            kind=kind,
            service=service,
            source=source,
            tool=tool,
            query=query[:2000],
            start=start,
            end=end,
            retrieved_at=retrieved_at,
            summary=summary[:1000],
            anomalous=anomalous,
            signals=signals,
            content_hash=content_hash,
            error=error,
        )


class CauseCategory(StrEnum):
    BAD_CONFIG_ROLLOUT = "bad_config_rollout"
    CRASH_LOOP_ROLLOUT = "crash_loop_rollout"
    FEATURE_FLAG_REGRESSION = "feature_flag_regression"
    DEPENDENCY_FAILURE = "dependency_failure"
    RESOURCE_SATURATION = "resource_saturation"
    UNKNOWN = "unknown"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    cause_category: CauseCategory
    suspected_service: str
    explanation: str = Field(max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=20)
    recommended_runbook_id: str | None = None
    runbook_params: dict[str, Any] = Field(default_factory=dict)
    uncertainty: str = Field(default="", max_length=1000)
    next_checks: list[str] = Field(default_factory=list, max_length=10)


class HypothesesOutput(BaseModel):
    hypotheses: list[Hypothesis] = Field(max_length=5)


class CriticVerdict(StrEnum):
    ACCEPTED = "accepted"
    DOWNGRADED = "downgraded"
    REJECTED = "rejected"


class CriticFinding(BaseModel):
    hypothesis_id: str
    verdict: CriticVerdict
    adjusted_confidence: float
    reasons: list[str]


class Triage(BaseModel):
    impact: str
    service: str
    criticality: str
    scope: list[str]
    window_start: AwareDatetime
    window_end: AwareDatetime
    notes: str = ""


class RemediationProposal(BaseModel):
    runbook_id: str
    params: dict[str, Any]
    risk: str
    hypothesis_id: str
    rationale: str
    evidence_ids: list[str]
    blast_radius: dict[str, Any]
    # Deployment revision observed during the investigation; approvals bind to it.
    target_revision: int | None = None


class AgentStep(BaseModel):
    """One entry in the agent activity timeline shown to SREs."""

    node: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    summary: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class InvestigationOutcome(StrEnum):
    PROPOSED = "proposed"  # remediation proposal ready for approval
    ESCALATED = "escalated"  # diagnosis without an allowlisted remediation
    ABSTAINED = "abstained"  # insufficient evidence
    FAILED = "failed"


class InvestigationResult(BaseModel):
    outcome: InvestigationOutcome
    summary: str
    triage: Triage | None
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    findings: list[CriticFinding]
    proposal: RemediationProposal | None
    steps: list[AgentStep]
    provider: str
    error: str | None = None
