from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sentinel_contracts import AlertStatus, Severity
from sentinel_ingestion.normalize import NormalizationError, normalize_webhook

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

WEBHOOK: dict[str, Any] = {
    "version": "4",
    "status": "firing",
    "receiver": "sentinelops-gateway",
    "groupKey": '{}:{alertname="CheckoutHigh5xxRate"}',
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "CheckoutHigh5xxRate",
                "service": "checkout",
                "severity": "critical",
                "environment": "local",
                "service_name": "checkout",
            },
            "annotations": {"summary": "checkout 5xx ratio above 5%"},
            "startsAt": "2026-10-09T11:58:42.736Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
            "fingerprint": "a1b2c3d4e5f60718",
        }
    ],
}


def _norm(payload: dict[str, Any]) -> tuple[list[Any], list[str]]:
    return normalize_webhook(payload, default_environment="local", received_at=NOW)


def test_normalizes_firing_alert() -> None:
    events, rejected = _norm(WEBHOOK)
    assert rejected == []
    (e,) = events
    assert e.status is AlertStatus.FIRING
    assert e.severity is Severity.CRITICAL
    assert e.fingerprint == "local:checkout:CheckoutHigh5xxRate"
    assert e.ends_at is None
    assert e.labels["am_fingerprint"] == "a1b2c3d4e5f60718"
    assert e.event_id.startswith("alertmanager:")


def test_redelivery_is_deterministic() -> None:
    a, _ = _norm(WEBHOOK)
    b, _ = normalize_webhook(WEBHOOK, default_environment="local", received_at=NOW.replace(hour=13))
    assert a[0].event_id == b[0].event_id


def test_resolved_gets_distinct_event_id_and_ends_at() -> None:
    resolved = copy.deepcopy(WEBHOOK)
    resolved["status"] = "resolved"
    resolved["alerts"][0]["status"] = "resolved"
    resolved["alerts"][0]["endsAt"] = "2026-10-09T12:10:00Z"
    (fire,), _ = _norm(WEBHOOK)
    (res,), _ = _norm(resolved)
    assert res.event_id != fire.event_id
    assert res.ends_at is not None
    assert res.fingerprint == fire.fingerprint


def test_service_falls_back_to_job_label() -> None:
    p = copy.deepcopy(WEBHOOK)
    labels = p["alerts"][0]["labels"]
    del labels["service"], labels["service_name"]
    labels["job"] = "inventory"
    (e,), _ = _norm(p)
    assert e.service == "inventory"


def test_alert_without_service_is_rejected_not_fatal() -> None:
    p = copy.deepcopy(WEBHOOK)
    good = copy.deepcopy(p["alerts"][0])
    bad = copy.deepcopy(p["alerts"][0])
    for k in ("service", "service_name"):
        del bad["labels"][k]
    p["alerts"] = [good, bad]
    events, rejected = _norm(p)
    assert len(events) == 1
    assert len(rejected) == 1


def test_unknown_severity_defaults_to_warning() -> None:
    p = copy.deepcopy(WEBHOOK)
    p["alerts"][0]["labels"]["severity"] = "bananas"
    (e,), _ = _norm(p)
    assert e.severity is Severity.WARNING


def test_label_values_are_sanitized_into_identifiers() -> None:
    p = copy.deepcopy(WEBHOOK)
    p["alerts"][0]["labels"]["service"] = "check out; DROP TABLE"
    (e,), _ = _norm(p)
    assert e.service == "check-out--DROP-TABLE"


def test_labels_and_values_bounded() -> None:
    p = copy.deepcopy(WEBHOOK)
    p["alerts"][0]["labels"].update({f"k{i:03d}": "v" * 5000 for i in range(200)})
    (e,), _ = _norm(p)
    assert len(e.labels) <= 64
    assert all(len(v) <= 1024 for v in e.labels.values())


def test_rejects_wrong_version() -> None:
    p = copy.deepcopy(WEBHOOK)
    p["version"] = "3"
    with pytest.raises(NormalizationError):
        _norm(p)


def test_rejects_too_many_alerts() -> None:
    p = copy.deepcopy(WEBHOOK)
    p["alerts"] = p["alerts"] * 501
    with pytest.raises(ValidationError):
        _norm(p)


def test_rejects_naive_timestamps() -> None:
    p = copy.deepcopy(WEBHOOK)
    p["alerts"][0]["startsAt"] = "2026-10-09T11:58:42"
    with pytest.raises(ValidationError):
        _norm(p)
