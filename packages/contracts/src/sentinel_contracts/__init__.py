"""Versioned contracts shared across SentinelOps services.

Contracts are the stable boundary between services: changing a field here is a
schema change and must bump ``schema_version`` (see docs/adr/ADR-004).
"""

from sentinel_contracts.events import SCHEMA_VERSION, AlertEvent, AlertSource, Severity
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
    "Incident",
    "IncidentStatus",
    "IncidentTransition",
    "InvalidTransitionError",
    "Severity",
    "Topic",
    "can_transition",
    "require_transition",
]
