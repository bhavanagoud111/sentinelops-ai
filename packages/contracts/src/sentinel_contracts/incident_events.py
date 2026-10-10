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
