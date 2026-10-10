"""Phase 2 exit gate: an alert produces exactly one durable, deduplicated incident;
redelivery, consumer replay and restarts never create duplicates.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from aiokafka import AIOKafkaProducer
from sentinel_contracts import AlertEvent, AlertSource, AlertStatus, Severity, Topic

from tests.integration.conftest import INGEST, KAFKA, alertmanager_webhook, compose, wait_for

pytestmark = pytest.mark.integration


def _post(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{INGEST}/api/v1/ingest/alertmanager",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert r.status_code == 202, r.text
    return dict(r.json())


def _incidents_for(api: httpx.Client, alertname: str) -> list[dict[str, Any]]:
    fp = f"local:inventory:{alertname}"
    items = api.get("/api/v1/incidents", params={"limit": 200}).json()["items"]
    return [i for i in items if i["fingerprint"] == fp]


def _produce(events: list[AlertEvent]) -> None:
    async def go() -> None:
        producer = AIOKafkaProducer(bootstrap_servers=KAFKA)
        await producer.start()
        try:
            for e in events:
                await producer.send_and_wait(
                    Topic.ALERTS.value,
                    json.dumps(e.model_dump(mode="json")).encode(),
                    key=e.fingerprint.encode(),
                )
        finally:
            await producer.stop()

    asyncio.run(go())


def _event(alertname: str, *, event_id: str | None = None) -> AlertEvent:
    now = datetime.now(UTC)
    return AlertEvent(
        event_id=event_id or f"synthetic:{uuid.uuid4().hex}",
        source=AlertSource.SYNTHETIC,
        status=AlertStatus.FIRING,
        alertname=alertname,
        fingerprint=AlertEvent.derive_fingerprint("local", "inventory", alertname),
        service="inventory",
        environment="local",
        severity=Severity.HIGH,
        starts_at=now,
        received_at=now,
    )


def test_duplicate_webhooks_create_one_incident(ingest_token: str, api: httpx.Client) -> None:
    name = f"DupTest{uuid.uuid4().hex[:8]}"
    payload = alertmanager_webhook(name, am_fingerprint="dup0000000000001")
    results = [_post(ingest_token, payload) for _ in range(5)]
    assert sum(r["accepted"] for r in results) == 1
    assert sum(r["duplicates"] for r in results) == 4
    incidents = wait_for(lambda: _incidents_for(api, name))
    assert len(incidents) == 1
    assert incidents[0]["status"] == "OPEN"
    assert incidents[0]["alert_count"] == 1


def test_consumer_redelivery_is_idempotent(api: httpx.Client) -> None:
    """Bypass the gateway: the same message delivered twice to Kafka is applied once."""
    name = f"Redeliver{uuid.uuid4().hex[:8]}"
    event = _event(name)
    _produce([event, event, event])
    incident = wait_for(lambda: _incidents_for(api, name))[0]
    # A distinct occurrence of the same condition attaches to the same incident.
    _produce([_event(name)])
    wait_for(lambda: _incidents_for(api, name)[0]["alert_count"] == 2)
    final = _incidents_for(api, name)
    assert len(final) == 1
    assert final[0]["id"] == incident["id"]


def test_resolved_alert_clears_but_does_not_close(ingest_token: str, api: httpx.Client) -> None:
    name = f"Resolve{uuid.uuid4().hex[:8]}"
    fp = uuid.uuid4().hex[:16]
    _post(ingest_token, alertmanager_webhook(name, am_fingerprint=fp))
    wait_for(lambda: _incidents_for(api, name))
    _post(ingest_token, alertmanager_webhook(name, am_fingerprint=fp, status="resolved"))
    inc = wait_for(
        lambda: next((i for i in _incidents_for(api, name) if not i["alert_active"]), None)
    )
    assert inc["status"] == "OPEN", "incident closes only via verification or a human"


def test_restart_and_full_replay_creates_no_duplicates(api: httpx.Client) -> None:
    name = f"Replay{uuid.uuid4().hex[:8]}"
    _produce([_event(name)])
    wait_for(lambda: _incidents_for(api, name))
    before = api.get("/api/v1/incidents", params={"limit": 200}).json()["items"]

    # Stop the correlator, rewind its consumer group to the beginning, restart it.
    compose("stop", "correlator")
    seek = compose(
        "exec",
        "-T",
        "redpanda",
        "rpk",
        "group",
        "seek",
        "sentinel-correlator",
        "--to",
        "start",
        check=False,
    )
    assert seek.returncode == 0, seek.stderr
    compose("start", "correlator")

    def lag_zero() -> bool:
        out = compose(
            "exec",
            "-T",
            "redpanda",
            "rpk",
            "group",
            "describe",
            "sentinel-correlator",
            check=False,
        ).stdout
        return "TOTAL-LAG" in out and any(
            line.split()[-1] == "0" for line in out.splitlines() if line.startswith("TOTAL-LAG")
        )

    wait_for(lag_zero, timeout=90, interval=2)
    after = api.get("/api/v1/incidents", params={"limit": 200}).json()["items"]
    assert len(after) == len(before), "replaying the whole topic must not create incidents"
    assert {i["id"]: i["alert_count"] for i in after} == {
        i["id"]: i["alert_count"] for i in before
    }, "replay must not re-attach already-applied alerts"
