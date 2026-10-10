from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sentinel_contracts import AlertEvent, AlertSource, AlertStatus, Severity
from sentinel_correlator.decide import Decision, IncidentRef, decide

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _event(status: AlertStatus = AlertStatus.FIRING, starts_at: datetime = T0) -> AlertEvent:
    return AlertEvent(
        event_id="alertmanager:x",
        source=AlertSource.ALERTMANAGER,
        status=status,
        alertname="CheckoutHigh5xxRate",
        fingerprint="local:checkout:CheckoutHigh5xxRate",
        service="checkout",
        environment="local",
        severity=Severity.CRITICAL,
        starts_at=starts_at,
        received_at=starts_at,
    )


OPEN = IncidentRef(status="INVESTIGATING", resolved_at=None)


def test_first_firing_creates() -> None:
    assert decide(_event(), None, None) is Decision.CREATE


def test_firing_with_open_incident_attaches() -> None:
    assert decide(_event(), OPEN, None) is Decision.ATTACH


@pytest.mark.parametrize(
    ("minutes", "expected"), [(5, Decision.REOPEN), (30, Decision.REOPEN), (31, Decision.CREATE)]
)
def test_reopen_window(minutes: int, expected: Decision) -> None:
    resolved = IncidentRef(status="RESOLVED", resolved_at=T0)
    event = _event(starts_at=T0 + timedelta(minutes=minutes))
    assert decide(event, None, resolved) is expected


def test_resolved_alert_clears_open_incident() -> None:
    assert decide(_event(AlertStatus.RESOLVED), OPEN, None) is Decision.CLEAR


def test_resolved_alert_without_incident_is_ignored() -> None:
    assert decide(_event(AlertStatus.RESOLVED), None, None) is Decision.IGNORE


def test_custom_window() -> None:
    resolved = IncidentRef(status="RESOLVED", resolved_at=T0)
    event = _event(starts_at=T0 + timedelta(minutes=10))
    assert decide(event, None, resolved, reopen_window=timedelta(minutes=5)) is Decision.CREATE
