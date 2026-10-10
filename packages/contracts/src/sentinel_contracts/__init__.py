"""Versioned contracts shared across SentinelOps services.

Contracts are the stable boundary between services: changing a field here is a
schema change and must bump ``schema_version`` (see docs/adr/ADR-004).
"""

from sentinel_contracts.events import (
    SCHEMA_VERSION,
    AlertEvent,
    AlertSource,
    AlertStatus,
    Severity,
)
from sentinel_contracts.incident_events import IncidentEvent, IncidentEventKind, InvestigationEvent
from sentinel_contracts.incidents import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    Incident,
    IncidentStatus,
    IncidentTransition,
    InvalidTransitionError,
    can_transition,
    require_transition,
)
from sentinel_contracts.topics import Topic

__all__ = [
    "ALLOWED_TRANSITIONS",
    "SCHEMA_VERSION",
    "TERMINAL_STATUSES",
    "AlertEvent",
    "AlertSource",
    "AlertStatus",
    "Incident",
    "IncidentEvent",
    "IncidentEventKind",
    "IncidentStatus",
    "IncidentTransition",
    "InvalidTransitionError",
    "InvestigationEvent",
    "Severity",
    "Topic",
    "can_transition",
    "require_transition",
]
