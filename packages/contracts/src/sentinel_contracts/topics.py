"""Kafka/Redpanda topic names (design spec section 7)."""

from enum import StrEnum


class Topic(StrEnum):
    ALERTS = "sentinel.alerts.v1"
    INCIDENTS = "sentinel.incidents.v1"
    INVESTIGATIONS = "sentinel.investigations.v1"
    APPROVALS = "sentinel.approvals.v1"
    ACTIONS = "sentinel.actions.v1"

    @property
    def dead_letter(self) -> str:
        """Dead-letter topic for messages a consumer could not process."""
        return f"{self.value}.dlq"
