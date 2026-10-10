"""Phase 4 exit gate, live API: rejection prevents execution, four-eyes, RBAC,
idempotency, and a stale approval blocked by the live executor's policy check.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tests.integration.conftest import API, PG, PG_PASSWORD, wait_for

pytestmark = pytest.mark.integration


def seed_awaiting_approval(target_revision: int = 999) -> str:
    """Insert an incident awaiting approval with a rollback proposal (test data)."""
    import asyncpg

    incident_id = uuid.uuid4()
    inv_id = uuid.uuid4()
    now = datetime.now(UTC)
    proposal = {
        "runbook_id": "rollback_deployment",
        "params": {"namespace": "sentinelops-demo", "deployment": "checkout", "to_revision": None},
        "risk": "medium",
        "hypothesis_id": "h1",
        "rationale": "test proposal",
        "evidence_ids": [],
        "blast_radius": {},
        "target_revision": target_revision,
    }

    async def go() -> None:
        conn = await asyncpg.connect(f"postgresql://sentinel:{PG_PASSWORD}@{PG}/sentinelops")
        try:
            await conn.execute(
                """INSERT INTO incidents (id, fingerprint, title, status, severity, service_name,
                   environment, alert_active, alert_count, first_seen, last_seen, summary, version)
                   VALUES ($1, $2, 'api test', 'AWAITING_APPROVAL', 'high', 'checkout', 'local',
                   true, 1, $3, $3, '', 1)""",
                incident_id,
                f"local:checkout:ApiTest{uuid.uuid4().hex[:8]}",
                now,
            )
            await conn.execute(
                """INSERT INTO investigations (id, incident_id, status, provider, trigger,
                   started_at, finished_at, summary, triage, steps, proposal, usage)
                   VALUES ($1, $2, 'proposed', 'rules', 'test', $3, $3, 'test', '{}', '[]',
                   $4::jsonb, '{}')""",
                inv_id,
                incident_id,
                now,
                json.dumps(proposal),
            )
        finally:
            await conn.close()

    asyncio.run(go())
    return str(incident_id)


def client(token_for: Callable[[str], str], user: str) -> httpx.Client:
    return httpx.Client(
        base_url=API, headers={"Authorization": f"Bearer {token_for(user)}"}, timeout=10
    )


def key() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4().hex}"}


def test_viewer_cannot_request_or_decide(token_for: Callable[[str], str]) -> None:
    inc = seed_awaiting_approval()
    viewer = client(token_for, "vera.viewer")
    assert (
        viewer.post(f"/api/v1/incidents/{inc}/approvals", json={}, headers=key()).status_code == 403
    )
    assert (
        viewer.post(
            f"/api/v1/approvals/{uuid.uuid4()}/decision",
            json={"decision": "approve", "reason": "nope"},
        ).status_code
        == 403
    )


def test_four_eyes_and_rejection_prevents_execution(token_for: Callable[[str], str]) -> None:
    inc = seed_awaiting_approval()
    sam = client(token_for, "sam.sre")  # holds operator AND approver roles
    appr = sam.post(f"/api/v1/incidents/{inc}/approvals", json={}, headers=key())
    assert appr.status_code == 201, appr.text
    aid = appr.json()["id"]
    own = sam.post(
        f"/api/v1/approvals/{aid}/decision", json={"decision": "approve", "reason": "self"}
    )
    assert own.status_code == 403, (
        "requester cannot approve their own request, even with both roles"
    )

    anna = client(token_for, "anna.approver")
    rej = anna.post(
        f"/api/v1/approvals/{aid}/decision", json={"decision": "reject", "reason": "risk too high"}
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == "rejected"

    oscar = client(token_for, "oscar.operator")
    ex = oscar.post(
        f"/api/v1/incidents/{inc}/actions/rollback_deployment/execute",
        json={"approval_id": aid},
        headers=key(),
    )
    assert ex.status_code == 409
    assert oscar.get(f"/api/v1/incidents/{inc}/executions").json() == []
    assert oscar.get(f"/api/v1/incidents/{inc}").json()["incident"]["status"] == "REJECTED"


def test_execute_requires_idempotency_key_and_matching_runbook(
    token_for: Callable[[str], str],
) -> None:
    inc = seed_awaiting_approval()
    oscar, anna = client(token_for, "oscar.operator"), client(token_for, "anna.approver")
    aid = oscar.post(f"/api/v1/incidents/{inc}/approvals", json={}, headers=key()).json()["id"]
    ok = anna.post(
        f"/api/v1/approvals/{aid}/decision",
        json={"decision": "approve", "reason": "evidence reviewed"},
    )
    assert ok.status_code == 200, ok.text
    no_key = oscar.post(
        f"/api/v1/incidents/{inc}/actions/rollback_deployment/execute", json={"approval_id": aid}
    )
    assert no_key.status_code == 422
    wrong = oscar.post(
        f"/api/v1/incidents/{inc}/actions/restart_deployment/execute",
        json={"approval_id": aid},
        headers=key(),
    )
    assert wrong.status_code == 409


def test_stale_approval_blocked_by_live_executor(token_for: Callable[[str], str]) -> None:
    """Approved against revision 999; the live deployment is at a different revision."""
    inc = seed_awaiting_approval(target_revision=999)
    oscar, anna = client(token_for, "oscar.operator"), client(token_for, "anna.approver")
    aid = oscar.post(f"/api/v1/incidents/{inc}/approvals", json={}, headers=key()).json()["id"]
    ok = anna.post(
        f"/api/v1/approvals/{aid}/decision",
        json={"decision": "approve", "reason": "evidence reviewed"},
    )
    assert ok.status_code == 200, ok.text
    k = key()
    first = oscar.post(
        f"/api/v1/incidents/{inc}/actions/rollback_deployment/execute",
        json={"approval_id": aid},
        headers=k,
    )
    assert first.status_code == 202, first.text
    replay = oscar.post(
        f"/api/v1/incidents/{inc}/actions/rollback_deployment/execute",
        json={"approval_id": aid},
        headers=k,
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"], "idempotent replay returns the same execution"

    def blocked() -> dict[str, Any] | None:
        execs = oscar.get(f"/api/v1/incidents/{inc}/executions").json()
        return execs[0] if execs and execs[0]["status"] != "pending" else None

    ex = wait_for(blocked, timeout=60, interval=1)
    assert ex["status"] == "blocked"
    failed = {c["name"] for c in ex["policy"]["checks"] if not c["passed"]}
    assert "no_revision_drift" in failed
    assert oscar.get(f"/api/v1/incidents/{inc}").json()["incident"]["status"] == "NEEDS_HUMAN"
