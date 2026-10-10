"""Model provider interface (ADR-008).

A provider turns an evidence bundle into hypotheses. It may request bounded
follow-up queries through the toolbox, but it never executes actions and its
output is always re-validated by the critic and the policy layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sentinel_contracts import Incident

from sentinel_investigator.models import CriticFinding, Evidence, HypothesesOutput, Triage
from sentinel_investigator.tools import Toolbox, ToolContext


@dataclass
class AnalysisInput:
    incident: Incident
    triage: Triage
    evidence: list[Evidence]
    catalog: list[dict[str, Any]]
    service_graph: dict[str, list[str]]


@dataclass
class ProviderResult:
    output: HypothesesOutput
    summary: str
    extra_evidence: list[Evidence] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str | None = None


class ModelProvider(Protocol):
    name: str

    async def hypothesize(
        self, data: AnalysisInput, toolbox: Toolbox, ctx: ToolContext, max_tool_calls: int
    ) -> ProviderResult: ...

    async def critique(
        self, data: AnalysisInput, output: HypothesesOutput
    ) -> tuple[list[CriticFinding], dict[str, int]]:
        """Optional model-side review. May only downgrade or reject, never add."""
        ...
