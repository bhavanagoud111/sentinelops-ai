"""Incident domain contract and state machine (design spec section 8).

The transition table is the single source of truth for legal status changes.
Persistence layers must call :func:`require_transition` and record an
:class:`IncidentTransition` for every change.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from sentinel_contracts.events import Identifier, Severity


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


S = IncidentStatus

# REOPEN in the spec is modelled as RESOLVED -> OPEN (recurrence of the same fingerprint).
ALLOWED_TRANSITIONS: MappingProxyType[IncidentStatus, frozenset[IncidentStatus]] = MappingProxyType(
    {
        S.OPEN: frozenset({S.TRIAGING, S.NEEDS_HUMAN}),
        S.TRIAGING: frozenset({S.INVESTIGATING, S.NEEDS_HUMAN}),
        S.INVESTIGATING: frozenset({S.AWAITING_APPROVAL, S.NEEDS_HUMAN}),
        S.AWAITING_APPROVAL: frozenset({S.REMEDIATING, S.REJECTED, S.NEEDS_HUMAN}),
        S.REMEDIATING: frozenset({S.VERIFYING, S.FAILED, S.NEEDS_HUMAN}),
        S.VERIFYING: frozenset({S.RESOLVED, S.FAILED, S.NEEDS_HUMAN}),
        S.RESOLVED: frozenset({S.OPEN}),
        S.NEEDS_HUMAN: frozenset({S.INVESTIGATING, S.RESOLVED}),
        S.REJECTED: frozenset({S.INVESTIGATING, S.NEEDS_HUMAN, S.RESOLVED}),
        S.FAILED: frozenset({S.NEEDS_HUMAN, S.INVESTIGATING}),
    }
)

TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset({S.RESOLVED})


class InvalidTransitionError(ValueError):
    def __init__(self, current: IncidentStatus, target: IncidentStatus) -> None:
        super().__init__(f"illegal incident transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: IncidentStatus, target: IncidentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def require_transition(current: IncidentStatus, target: IncidentStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


Actor = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class Incident(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    fingerprint: Identifier
    status: IncidentStatus
    severity: Severity
    service: Identifier
    environment: Identifier
    first_seen: AwareDatetime
    last_seen: AwareDatetime
    summary: Annotated[str, Field(max_length=2000)] = ""
    version: Annotated[int, Field(ge=0, description="Optimistic-lock version")] = 0


class IncidentTransition(BaseModel):
    """Audit record persisted for every status change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: UUID
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    actor: Actor
    reason: Annotated[str, Field(max_length=1000)]
    correlation_id: Identifier
    at: AwareDatetime
