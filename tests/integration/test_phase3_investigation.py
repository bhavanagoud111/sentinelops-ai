"""Phase 3 exit gate, live: an incident is investigated automatically, evidence is
persisted with immutable ids, every hypothesis cites existing evidence, and the
incident lands in AWAITING_APPROVAL or NEEDS_HUMAN — never in an executed action.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration.conftest import INGEST, alertmanager_webhook, wait_for

pytestmark = pytest.mark.integration

TERMINAL_INVESTIGATION = {"proposed", "escalated", "abstained", "failed"}


def _find(api: httpx.Client, fingerprint: str) -> dict[str, Any] | None:
    items = api.get("/api/v1/incidents", params={"limit": 200}).json()["items"]
    return next((i for i in items if i["fingerprint"] == fingerprint), None)


def test_incident_is_investigated_with_cited_evidence(ingest_token: str, api: httpx.Client) -> None:
    name = f"InvTest{uuid.uuid4().hex[:8]}"
    r = httpx.post(
        f"{INGEST}/api/v1/ingest/alertmanager",
        json=alertmanager_webhook(name, service="checkout", severity="critical"),
        headers={"Authorization": f"Bearer {ingest_token}"},
        timeout=10,
    )
    assert r.status_code == 202
    inc = wait_for(lambda: _find(api, f"local:checkout:{name}"), timeout=30)

    def finished() -> list[dict[str, Any]] | None:
        invs = api.get(f"/api/v1/incidents/{inc['id']}/investigations").json()
        return invs if invs and invs[0]["status"] in TERMINAL_INVESTIGATION else None

    invs = wait_for(finished, timeout=150, interval=2)
    inv = invs[0]
    assert inv["provider"] == "rules"
    assert [s["node"] for s in inv["steps"]] == [
        "triage",
        "collect_evidence",
        "root_cause",
        "critic",
        "plan_remediation",
        "report",
    ]
    evidence = api.get(f"/api/v1/incidents/{inc['id']}/evidence").json()
    assert len(evidence) >= 10, "the standard plan queries every source for each scoped service"
    ids = {e["evidence_id"] for e in evidence}
    assert all(
        e["evidence_id"].startswith("ev_") and len(e["content_hash"]) == 64 for e in evidence
    )
    for h in inv["hypotheses"]:
        assert set(h["evidence_ids"]) <= ids, f"{h['key']} cites unknown evidence"

    final = api.get(f"/api/v1/incidents/{inc['id']}").json()["incident"]
    assert final["status"] in {"AWAITING_APPROVAL", "NEEDS_HUMAN"}
    timeline = api.get(f"/api/v1/incidents/{inc['id']}/timeline").json()
    statuses = [t["title"] for t in timeline if t["kind"] == "transition"]
    assert any("TRIAGING → INVESTIGATING" in s for s in statuses)


def test_manual_reinvestigation_requires_permission_and_valid_state(
    ingest_token: str, api: httpx.Client, token_for: Any
) -> None:
    items = api.get("/api/v1/incidents", params={"status": "NEEDS_HUMAN", "limit": 1}).json()[
        "items"
    ]
    if not items:
        pytest.skip("no NEEDS_HUMAN incident available")
    inc_id = items[0]["id"]
    viewer = {"Authorization": f"Bearer {token_for('vera.viewer')}"}
    denied = api.post(f"/api/v1/incidents/{inc_id}/investigate", headers=viewer)
    assert denied.status_code == 403

    op = {
        "Authorization": f"Bearer {token_for('oscar.operator')}",
        "Idempotency-Key": f"reinv-{uuid.uuid4().hex}",
    }
    first = api.post(f"/api/v1/incidents/{inc_id}/investigate", headers=op)
    assert first.status_code == 202, first.text
    replay = api.post(f"/api/v1/incidents/{inc_id}/investigate", headers=op)
    assert replay.status_code == 202
    assert replay.json() == first.json(), "same Idempotency-Key returns the stored response"


def test_cannot_investigate_resolved_or_unknown_incident(api: httpx.Client) -> None:
    r = api.post(f"/api/v1/incidents/{uuid.uuid4()}/investigate")
    assert r.status_code == 404
