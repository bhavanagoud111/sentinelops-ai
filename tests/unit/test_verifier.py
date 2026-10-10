from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sentinel_investigator.models import Evidence, EvidenceKind
from sentinel_verifier.main import healthy, symptom_kinds

K = EvidenceKind


def ev(kind: EvidenceKind, signals: dict[str, Any], error: str | None = None) -> Evidence:
    now = datetime.now(UTC)
    return Evidence.make(
        incident_id=uuid.uuid4(),
        kind=kind,
        service="checkout",
        source="prometheus",
        tool="t",
        query="q",
        start=now,
        end=now,
        retrieved_at=now,
        summary="s",
        anomalous=False,
        signals=signals,
        error=error,
    )


@pytest.mark.parametrize(
    ("alert", "kinds"),
    [
        ("CheckoutHigh5xxRate", [K.ERROR_RATIO]),
        ("InventoryHighLatencyP95", [K.LATENCY_P95]),
        ("DeploymentCrashLooping", [K.POD_STATUS]),
        ("WorkerMemoryHigh", [K.PROCESS_MEMORY]),
        ("SomethingElse", [K.ERROR_RATIO, K.LATENCY_P95]),
    ],
)
def test_symptom_mapping(alert: str, kinds: list[EvidenceKind]) -> None:
    assert symptom_kinds(alert) == kinds


def test_metric_recovered_when_no_longer_active() -> None:
    assert healthy(ev(K.ERROR_RATIO, {"active": False, "peak": 1.0}))[0]
    assert not healthy(ev(K.ERROR_RATIO, {"active": True}))[0]


def test_pods_healthy_requires_all_ready_and_no_waiting() -> None:
    assert healthy(ev(K.POD_STATUS, {"ready": 2, "desired": 2, "waiting_reasons": []}))[0]
    assert not healthy(ev(K.POD_STATUS, {"ready": 1, "desired": 2, "waiting_reasons": []}))[0]
    assert not healthy(
        ev(K.POD_STATUS, {"ready": 2, "desired": 2, "waiting_reasons": ["CrashLoopBackOff"]})
    )[0]
    assert not healthy(ev(K.POD_STATUS, {"ready": 0, "desired": 0, "waiting_reasons": []}))[0]


def test_unavailable_evidence_is_never_healthy() -> None:
    assert not healthy(ev(K.ERROR_RATIO, {"active": False}, error="prometheus down"))[0]


def test_crash_looping_flag_is_unhealthy_even_between_restarts() -> None:
    sig = {"ready": 2, "desired": 2, "waiting_reasons": [], "crash_looping": True}
    assert not healthy(ev(K.POD_STATUS, sig))[0]
