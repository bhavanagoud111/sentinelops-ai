"""Correlation policy (ADR-010). Pure: no I/O.

Correlation key = ``environment:service:alertname`` (``AlertEvent.fingerprint``).

firing   + open incident with same key            → ATTACH
firing   + key resolved within the reopen window   → REOPEN
firing   + otherwise                               → CREATE
resolved + open incident                           → CLEAR (alert no longer firing;
                                                     the incident itself is only closed
                                                     by verification or a human)
resolved + no open incident                        → IGNORE
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sentinel_contracts import AlertEvent, AlertStatus

DEFAULT_REOPEN_WINDOW = timedelta(minutes=30)


class Decision(StrEnum):
    CREATE = "create"
    ATTACH = "attach"
    REOPEN = "reopen"
    CLEAR = "clear"
    IGNORE = "ignore"


@dataclass(frozen=True)
class IncidentRef:
    status: str
    resolved_at: datetime | None


def decide(
    event: AlertEvent,
    open_incident: IncidentRef | None,
    last_resolved: IncidentRef | None,
    reopen_window: timedelta = DEFAULT_REOPEN_WINDOW,
) -> Decision:
    if event.status is AlertStatus.RESOLVED:
        return Decision.CLEAR if open_incident else Decision.IGNORE
    if open_incident:
        return Decision.ATTACH
    if (
        last_resolved
        and last_resolved.resolved_at
        and event.starts_at - last_resolved.resolved_at <= reopen_window
    ):
        return Decision.REOPEN
    return Decision.CREATE
