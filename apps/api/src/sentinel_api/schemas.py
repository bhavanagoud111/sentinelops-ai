"""API response/request models. Kept separate from ORM rows and event contracts."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sentinel_contracts import Incident


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class ServiceOut(BaseModel):
    name: str
    namespace: str
    owner: str
    criticality: str
    description: str
    dependencies: list[str]


class TimelineEntry(BaseModel):
    at: datetime
    kind: Literal["transition", "alert", "investigation", "approval", "execution", "audit"]
    title: str
    actor: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AlertEventOut(BaseModel):
    event_id: str
    status: str
    received_at: datetime
    alertname: str
    severity: str
    starts_at: datetime
    labels: dict[str, str]
    annotations: dict[str, str]


class IncidentDetail(BaseModel):
    incident: Incident
    alerts: list[AlertEventOut]
    service: ServiceOut | None = None


class TokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    subject: str
    name: str
    roles: list[str]
    permissions: list[str]


class ErrorOut(BaseModel):
    detail: str


def encode_cursor(first_seen: datetime, incident_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{first_seen.isoformat()}|{incident_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts, _, ident = raw.partition("|")
    return datetime.fromisoformat(ts), uuid.UUID(ident)
