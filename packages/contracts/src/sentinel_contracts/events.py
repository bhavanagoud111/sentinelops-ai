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


class AlertSource(StrEnum):
    ALERTMANAGER = "alertmanager"
    KUBERNETES = "kubernetes"
    SYNTHETIC = "synthetic"


class AlertEvent(BaseModel):
    """A single normalized alert occurrence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: Identifier = Field(description="Globally unique, deterministic id used for dedup")
    source: AlertSource
    fingerprint: Identifier = Field(description="Stable identity of the alerting condition")
    service: Identifier
    environment: Identifier
    severity: Severity
    starts_at: AwareDatetime
    labels: LabelMap = Field(default_factory=dict, max_length=64)
    annotations: LabelMap = Field(default_factory=dict, max_length=32)
    received_at: AwareDatetime

    @staticmethod
    def derive_event_id(source: AlertSource, fingerprint: str, starts_at: datetime) -> str:
        """Deterministic id: the same alert occurrence redelivered yields the same id.

        Alertmanager re-sends firing alerts on every ``repeat_interval``; keying on
        (source, fingerprint, starts_at) makes those resends idempotent.
        """
        raw = json.dumps(
            [source.value, fingerprint, starts_at.isoformat()], separators=(",", ":")
        ).encode()
        return f"{source.value}:{hashlib.sha256(raw).hexdigest()[:32]}"
