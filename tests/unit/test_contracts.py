from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sentinel_contracts import (
    ALLOWED_TRANSITIONS,
    AlertEvent,
    AlertSource,
    AlertStatus,
    IncidentStatus,
    InvalidTransitionError,
    Severity,
    Topic,
    can_transition,
    require_transition,
)

S = IncidentStatus
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _event(**overrides: object) -> AlertEvent:
    data: dict[str, object] = {
        "event_id": "alertmanager:abc",
        "source": AlertSource.ALERTMANAGER,
        "status": AlertStatus.FIRING,
        "alertname": "CheckoutHigh5xxRate",
        "fingerprint": "local:checkout:CheckoutHigh5xxRate",
        "service": "checkout",
        "environment": "local",
        "severity": Severity.CRITICAL,
        "starts_at": NOW,
        "received_at": NOW,
    }
    data.update(overrides)
    return AlertEvent.model_validate(data)


class TestStateMachine:
    def test_every_status_has_transition_entry(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(IncidentStatus)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (S.OPEN, S.TRIAGING),
            (S.TRIAGING, S.INVESTIGATING),
            (S.INVESTIGATING, S.AWAITING_APPROVAL),
            (S.AWAITING_APPROVAL, S.REMEDIATING),
            (S.REMEDIATING, S.VERIFYING),
            (S.VERIFYING, S.RESOLVED),
        ],
    )
    def test_happy_path(self, current: IncidentStatus, target: IncidentStatus) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (S.INVESTIGATING, S.NEEDS_HUMAN),
            (S.AWAITING_APPROVAL, S.REJECTED),
            (S.REMEDIATING, S.FAILED),
            (S.VERIFYING, S.FAILED),
            (S.VERIFYING, S.NEEDS_HUMAN),
            (S.RESOLVED, S.OPEN),  # reopen on recurrence
        ],
    )
    def test_alternative_paths(self, current: IncidentStatus, target: IncidentStatus) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (S.OPEN, S.REMEDIATING),  # cannot skip approval
            (S.INVESTIGATING, S.REMEDIATING),  # cannot skip approval
            (S.REJECTED, S.REMEDIATING),  # rejection must not lead to execution
            (S.RESOLVED, S.REMEDIATING),
            (S.OPEN, S.OPEN),
        ],
    )
    def test_illegal_transitions(self, current: IncidentStatus, target: IncidentStatus) -> None:
        assert not can_transition(current, target)
        with pytest.raises(InvalidTransitionError):
            require_transition(current, target)

    def test_remediating_only_reachable_from_awaiting_approval(self) -> None:
        sources = {src for src, targets in ALLOWED_TRANSITIONS.items() if S.REMEDIATING in targets}
        assert sources == {S.AWAITING_APPROVAL}


class TestAlertEvent:
    def test_event_id_is_deterministic(self) -> None:
        a = AlertEvent.derive_event_id(AlertSource.ALERTMANAGER, "fp-1", NOW, AlertStatus.FIRING)
        b = AlertEvent.derive_event_id(AlertSource.ALERTMANAGER, "fp-1", NOW, AlertStatus.FIRING)
        assert a == b
        assert a.startswith("alertmanager:")

    def test_event_id_differs_per_occurrence(self) -> None:
        later = NOW.replace(minute=5)
        fire = AlertStatus.FIRING
        assert AlertEvent.derive_event_id(
            AlertSource.ALERTMANAGER, "fp-1", NOW, fire
        ) != AlertEvent.derive_event_id(AlertSource.ALERTMANAGER, "fp-1", later, fire)

    def test_event_id_differs_between_firing_and_resolved(self) -> None:
        src = AlertSource.ALERTMANAGER
        assert AlertEvent.derive_event_id(
            src, "fp-1", NOW, AlertStatus.FIRING
        ) != AlertEvent.derive_event_id(src, "fp-1", NOW, AlertStatus.RESOLVED)

    def test_fingerprint_is_correlation_key(self) -> None:
        fp = AlertEvent.derive_fingerprint("local", "checkout", "CheckoutHigh5xxRate")
        assert fp == "local:checkout:CheckoutHigh5xxRate"

    def test_severity_rank_orders(self) -> None:
        assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.WARNING.rank

    def test_valid_event_round_trips(self) -> None:
        event = _event(labels={"alertname": "CheckoutHigh5xxRate"})
        assert AlertEvent.model_validate_json(event.model_dump_json()) == event
        assert event.schema_version == 1

    def test_rejects_naive_datetimes(self) -> None:
        with pytest.raises(ValidationError):
            _event(starts_at=datetime(2026, 10, 9, 12, 0))

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            _event(unexpected="x")

    @pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "semi;colon"])
    def test_rejects_bad_identifiers(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            _event(service=bad)

    def test_bounds_label_count(self) -> None:
        with pytest.raises(ValidationError):
            _event(labels={f"k{i}": "v" for i in range(65)})


def test_dead_letter_topic_name() -> None:
    assert Topic.ALERTS.dead_letter == "sentinel.alerts.v1.dlq"


def test_committed_schemas_are_current() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "export_schemas.py"
    spec = importlib.util.spec_from_file_location("export_schemas", script)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0, "run `make schemas` and commit the result"
