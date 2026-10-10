"""Incident lifecycle events published to ``sentinel.incidents.v1``."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from sentinel_contracts.events import SCHEMA_VERSION, Identifier
from sentinel_contracts.incidents import Incident, IncidentStatus


class IncidentEventKind(StrEnum):
    CREATED = "created"
    ALERT_ATTACHED = "alert_attached"
    ALERT_CLEARED = "alert_cleared"
    REOPENED = "reopened"
    STATUS_CHANGED = "status_changed"
    INVESTIGATION_REQUESTED = "investigation_requested"


class IncidentEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Identifier
    kind: IncidentEventKind
    incident: Incident
    previous_status: IncidentStatus | None = None
    cause_event_id: Identifier | None = None
    correlation_id: Identifier
    occurred_at: AwareDatetime

    @classmethod
    def build(
        cls,
        kind: IncidentEventKind,
        incident: Incident,
        *,
        occurred_at: datetime,
        correlation_id: str,
        previous_status: IncidentStatus | None = None,
        cause_event_id: str | None = None,
    ) -> IncidentEvent:
        return cls(
            event_id=f"incident:{uuid.uuid4().hex}",
            kind=kind,
            incident=incident,
            previous_status=previous_status,
            cause_event_id=cause_event_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
        )


class InvestigationEvent(BaseModel):
    """Published to ``sentinel.investigations.v1`` when an investigation finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Identifier
    incident_id: uuid.UUID
    investigation_id: uuid.UUID
    outcome: Literal["proposed", "escalated", "abstained", "failed"]
    provider: str
    summary: str
    proposal: dict[str, object] | None = None
    top_hypothesis: dict[str, object] | None = None
    correlation_id: Identifier
    occurred_at: AwareDatetime


class ApprovalEvent(BaseModel):
    """Published to ``sentinel.approvals.v1`` on request and decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Identifier
    approval_id: uuid.UUID
    incident_id: uuid.UUID
    runbook_id: str
    params: dict[str, object]
    status: Literal["pending", "approved", "rejected", "expired", "consumed"]
    actor: str
    reason: str | None = None
    occurred_at: AwareDatetime


class ActionEventKind(StrEnum):
    REQUESTED = "requested"
    BLOCKED = "blocked"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"


class ActionEvent(BaseModel):
    """Published to ``sentinel.actions.v1`` across the execution lifecycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Identifier
    kind: ActionEventKind
    execution_id: uuid.UUID
    incident_id: uuid.UUID
    approval_id: uuid.UUID
    runbook_id: str
    actor: str
    details: dict[str, object] = {}
    occurred_at: AwareDatetime
