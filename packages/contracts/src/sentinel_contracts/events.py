"""Normalized alert event contract (design spec section 7).

Every inbound alert, regardless of source, is normalized into an ``AlertEvent``
before it is written to the outbox and published to ``sentinel.alerts.v1``.
Consumers deduplicate on ``event_id`` (at-least-once delivery).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION: Literal[1] = 1

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:/-]+$")
]
LabelMap = dict[
    Annotated[str, StringConstraints(max_length=128)], Annotated[str, Field(max_length=1024)]
]


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Higher is more severe; used to escalate an incident's severity."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class AlertSource(StrEnum):
    ALERTMANAGER = "alertmanager"
    KUBERNETES = "kubernetes"
    SYNTHETIC = "synthetic"


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class AlertEvent(BaseModel):
    """A single normalized alert occurrence (one status change of one alert)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Identifier = Field(description="Deterministic id used for deduplication")
    source: AlertSource
    status: AlertStatus
    alertname: Identifier
    fingerprint: Identifier = Field(
        description="Correlation key of the alerting condition: environment:service:alertname"
    )
    service: Identifier
    environment: Identifier
    severity: Severity
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    labels: LabelMap = Field(default_factory=dict, max_length=64)
    annotations: LabelMap = Field(default_factory=dict, max_length=32)
    received_at: AwareDatetime

    @staticmethod
    def derive_fingerprint(environment: str, service: str, alertname: str) -> str:
        """Correlation key: alerts with the same key belong to the same incident."""
        return f"{environment}:{service}:{alertname}"

    @staticmethod
    def derive_event_id(
        source: AlertSource, source_fingerprint: str, starts_at: datetime, status: AlertStatus
    ) -> str:
        """Deterministic id: the same alert status change redelivered yields the same id.

        Alertmanager re-sends firing alerts on every ``repeat_interval``; keying on
        (source, source fingerprint, starts_at, status) makes those resends idempotent
        while keeping the later "resolved" notification distinct.
        """
        raw = json.dumps(
            [source.value, source_fingerprint, starts_at.isoformat(), status.value],
            separators=(",", ":"),
        ).encode()
        return f"{source.value}:{hashlib.sha256(raw).hexdigest()[:32]}"
