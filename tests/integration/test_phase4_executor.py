"""Phase 4 exit gate (deterministic): executor + policy against a real database,
with a fake cluster so every safety path is exercised without touching Kubernetes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sentinel_contracts import ActionEvent, ActionEventKind, Topic
from sentinel_executor.k8s_actions import ActionResult
from sentinel_executor.main import Executor
from sentinel_policy import RunbookParams
from sentinel_policy.engine import DeploymentSnapshot, RemediationMode, compute_request_hash
from sentinel_storage import audit
from sentinel_storage.db import transaction
from sentinel_storage.models import ApprovalRow, ExecutionRow, OutboxEvent
from sentinel_storage.models import Incident as IncidentRow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration
Sessions = async_sessionmaker[AsyncSession]


@dataclass
class FakeCluster:
    revision: int = 3
    revisions: tuple[int, ...] = (1, 2, 3)
    fail: bool = False
    applied: list[RunbookParams] = field(default_factory=list)

    async def snapshot(self, namespace: str, name: str) -> DeploymentSnapshot:
        return DeploymentSnapshot(
            namespace=namespace,
            name=name,
            exists=True,
            revision=self.revision,
            replicas=2,
            revisions=self.revisions,
        )

    async def apply(self, params: RunbookParams, rollout_timeout_s: float = 180) -> ActionResult:
        if self.fail:
            raise RuntimeError("rollout stuck")
        self.applied.append(params)
        return ActionResult(
            command_template=f"kubectl rollout undo deployment/{params.deployment}",
            patch=[],
            before={"revision": self.revision},
            after={"revision": self.revision + 1},
            rollout_seconds=1.0,
        )


async def setup(
    db: Sessions,
    *,
    params: dict[str, Any] | None = None,
    expires_in: timedelta = timedelta(minutes=10),
    target_revision: int = 3,
    approved_by: str = "anna.approver",
) -> tuple[uuid.UUID, ActionEvent]:
    now = datetime.now(UTC)
    params = params or {"namespace": "sentinelops-demo", "deployment": "checkout"}
    async with transaction(db) as s:
        inc = IncidentRow(
            fingerprint=f"local:checkout:T{uuid.uuid4().hex[:6]}",
            title="t",
            status="REMEDIATING",
            severity="critical",
            service_name="checkout",
            environment="local",
            alert_active=True,
            alert_count=1,
            first_seen=now,
            last_seen=now,
            summary="",
        )
        s.add(inc)
        await s.flush()
        a = ApprovalRow(
            incident_id=inc.id,
            runbook_id="rollback_deployment",
            params=params,
            rationale="r",
            risk="medium",
            target_revision=target_revision,
            request_hash=compute_request_hash(
                inc.id, "rollback_deployment", params, target_revision
            )
            if params.get("namespace") == "sentinelops-demo"
            else "x" * 64,
            status="consumed",
            requested_by="oscar.operator",
            requested_at=now,
            approved_by=approved_by,
            decided_at=now,
            expires_at=now + expires_in,
        )
        s.add(a)
        await s.flush()
        ex = ExecutionRow(
            incident_id=inc.id,
            approval_id=a.id,
            runbook_id="rollback_deployment",
            target=f"{params.get('namespace')}/{params['deployment']}",
            params=params,
            status="pending",
            idempotency_key=uuid.uuid4().hex,
            requested_by="oscar.operator",
            policy={},
            command_template="",
            before_state={},
            after_state={},
            verification={},
        )
        s.add(ex)
        await s.flush()
        event = ActionEvent(
            event_id=f"action:{ex.id}:requested",
            kind=ActionEventKind.REQUESTED,
            execution_id=ex.id,
            incident_id=inc.id,
            approval_id=a.id,
            runbook_id="rollback_deployment",
            actor="oscar.operator",
            occurred_at=now,
        )
    return ex.id, event


async def state(db: Sessions, execution_id: uuid.UUID) -> tuple[ExecutionRow, IncidentRow]:
    async with db() as s:
        ex = await s.scalar(select(ExecutionRow).where(ExecutionRow.id == execution_id))
        assert ex is not None
        inc = await s.scalar(select(IncidentRow).where(IncidentRow.id == ex.incident_id))
        assert inc is not None
        return ex, inc


def failed_checks(ex: ExecutionRow) -> set[str]:
    return {c["name"] for c in ex.policy.get("checks", []) if not c["passed"]}


async def test_approved_action_executes_and_is_audited(db: Sessions) -> None:
    cluster = FakeCluster()
    ex_id, event = await setup(db)
    assert await Executor(db, cluster, RemediationMode.SANDBOX).handle(event) == "succeeded"
    ex, inc = await state(db, ex_id)
    assert ex.status == "succeeded"
    assert inc.status == "VERIFYING"
    assert len(cluster.applied) == 1
    async with db() as s:
        assert await audit.verify_chain(s) is None
        kinds = [
            r.payload["kind"]
            for r in (
                await s.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.topic == Topic.ACTIONS)
                    .order_by(OutboxEvent.id)
                )
            ).all()
        ]
    assert kinds == ["started", "succeeded"]


async def test_duplicate_delivery_executes_once(db: Sessions) -> None:
    cluster = FakeCluster()
    _, event = await setup(db)
    ex = Executor(db, cluster, RemediationMode.SANDBOX)
    await ex.handle(event)
    assert await ex.handle(event) is None
    assert len(cluster.applied) == 1


async def test_expired_approval_is_blocked(db: Sessions) -> None:
    cluster = FakeCluster()
    ex_id, event = await setup(db, expires_in=timedelta(seconds=-1))
    assert await Executor(db, cluster, RemediationMode.SANDBOX).handle(event) == "blocked"
    ex, inc = await state(db, ex_id)
    assert "approval_not_expired" in failed_checks(ex)
    assert inc.status == "NEEDS_HUMAN"
    assert cluster.applied == []


async def test_cross_namespace_action_is_blocked(db: Sessions) -> None:
    cluster = FakeCluster()
    ex_id, event = await setup(db, params={"namespace": "kube-system", "deployment": "coredns"})
    assert await Executor(db, cluster, RemediationMode.SANDBOX).handle(event) == "blocked"
    ex, _ = await state(db, ex_id)
    assert {"namespace_allowed", "deployment_allowed"} <= failed_checks(ex)
    assert cluster.applied == []


async def test_revision_drift_is_blocked(db: Sessions) -> None:
    cluster = FakeCluster(revision=4, revisions=(1, 2, 3, 4))  # someone deployed after approval
    ex_id, event = await setup(db, target_revision=3)
    assert await Executor(db, cluster, RemediationMode.SANDBOX).handle(event) == "blocked"
    assert "no_revision_drift" in failed_checks((await state(db, ex_id))[0])
    assert cluster.applied == []


async def test_recommend_only_mode_never_writes(db: Sessions) -> None:
    cluster = FakeCluster()
    ex_id, event = await setup(db)
    assert await Executor(db, cluster, RemediationMode.RECOMMEND_ONLY).handle(event) == "blocked"
    assert "mode_allows_writes" in failed_checks((await state(db, ex_id))[0])
    assert cluster.applied == []


async def test_cluster_failure_marks_failed(db: Sessions) -> None:
    ex_id, event = await setup(db)
    assert (
        await Executor(db, FakeCluster(fail=True), RemediationMode.SANDBOX).handle(event)
        == "failed"
    )
    ex, inc = await state(db, ex_id)
    assert ex.status == "failed"
    assert "rollout stuck" in (ex.error or "")
    assert inc.status == "FAILED"


async def test_database_enforces_four_eyes(db: Sessions) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await setup(db, approved_by="oscar.operator")
