"""Approval and execution endpoints (design spec sections 11 and 13).

Flow:  investigation proposal
    → POST /incidents/{id}/approvals            (operator requests; binds hash + revision)
    → POST /approvals/{id}/decision             (a *different* approver decides)
    → POST /incidents/{id}/actions/{rb}/execute (operator; Idempotency-Key required)
    → executor re-checks policy against a live snapshot, then acts; verifier confirms.

The API never touches the cluster. It records intent; the executor enforces policy.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sentinel_contracts import (
    ActionEvent,
    ActionEventKind,
    ApprovalEvent,
    IncidentStatus,
    Topic,
)
from sentinel_policy import CATALOG, RunbookId
from sentinel_policy.engine import compute_request_hash, normalize_params
from sentinel_storage import audit, outbox
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import ApprovalRow, ExecutionRow, InvestigationRow
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_api import idempotency
from sentinel_api.auth import ApprovalDecider, ApprovalRequester, Executor, Reader
from sentinel_api.deps import DbSession
from sentinel_api.routes.incidents import load_incident

router = APIRouter(prefix="/api/v1", tags=["remediation"])
APPROVAL_TTL = timedelta(minutes=int(os.environ.get("APPROVAL_TTL_MINUTES", "15")))


class ApprovalOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    investigation_id: uuid.UUID | None
    runbook_id: str
    runbook_title: str
    params: dict[str, Any]
    rationale: str
    risk: str
    target_revision: int | None
    status: str
    requested_by: str
    requested_at: datetime
    approved_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    expires_at: datetime
    request_hash: str


class ApprovalRequest(BaseModel):
    investigation_id: uuid.UUID | None = Field(
        default=None, description="Defaults to the latest investigation with a proposal"
    )


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=1000)


class ExecuteRequest(BaseModel):
    approval_id: uuid.UUID


class ExecutionOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    approval_id: uuid.UUID
    runbook_id: str
    target: str
    params: dict[str, Any]
    status: str
    requested_by: str
    policy: dict[str, Any]
    command_template: str
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    verification: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _approval_out(a: ApprovalRow) -> ApprovalOut:
    rb = CATALOG.get(RunbookId(a.runbook_id))
    return ApprovalOut(
        id=a.id,
        incident_id=a.incident_id,
        investigation_id=a.investigation_id,
        runbook_id=a.runbook_id,
        runbook_title=rb.title if rb else a.runbook_id,
        params=a.params,
        rationale=a.rationale,
        risk=a.risk,
        target_revision=a.target_revision,
        status=a.status,
        requested_by=a.requested_by,
        requested_at=a.requested_at,
        approved_by=a.approved_by,
        decided_at=a.decided_at,
        decision_reason=a.decision_reason,
        expires_at=a.expires_at,
        request_hash=a.request_hash,
    )


def _execution_out(e: ExecutionRow) -> ExecutionOut:
    return ExecutionOut(
        id=e.id,
        incident_id=e.incident_id,
        approval_id=e.approval_id,
        runbook_id=e.runbook_id,
        target=e.target,
        params=e.params,
        status=e.status,
        requested_by=e.requested_by,
        policy=e.policy,
        command_template=e.command_template,
        before_state=e.before_state,
        after_state=e.after_state,
        verification=e.verification,
        error=e.error,
        created_at=e.created_at,
        started_at=e.started_at,
        finished_at=e.finished_at,
    )


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.sessions
    return factory


async def _expire_if_needed(tx: AsyncSession, a: ApprovalRow, now: datetime) -> None:
    if a.status in ("pending", "approved") and now >= a.expires_at:
        a.status = "expired"
        await tx.flush()


# ------------------------------------------------------------------ approvals
@router.post(
    "/incidents/{incident_id}/approvals",
    status_code=status.HTTP_201_CREATED,
    response_model=ApprovalOut,
)
async def request_approval(
    incident_id: uuid.UUID,
    body: ApprovalRequest,
    principal: ApprovalRequester,
    request: Request,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ApprovalOut | JSONResponse:
    route = f"POST /incidents/{incident_id}/approvals"
    payload = body.model_dump(mode="json")
    prior = await idempotency.lookup(session, principal.subject, idempotency_key, route, payload)
    if prior:
        return JSONResponse(prior.response, status_code=prior.status_code)
    now = datetime.now(UTC)
    async with transaction(_sessions(request)) as tx:
        try:
            inc = await repo.get(tx, incident_id, lock=True)
        except repo.IncidentNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found") from None
        if inc.status != IncidentStatus.AWAITING_APPROVAL.value:
            raise HTTPException(status.HTTP_409_CONFLICT, f"incident is {inc.status}")
        q = select(InvestigationRow).where(
            InvestigationRow.incident_id == incident_id, InvestigationRow.status == "proposed"
        )
        if body.investigation_id:
            q = q.where(InvestigationRow.id == body.investigation_id)
        inv = await tx.scalar(q.order_by(InvestigationRow.finished_at.desc()).limit(1))
        if inv is None or not inv.proposal:
            raise HTTPException(status.HTTP_409_CONFLICT, "no remediation proposal to approve")
        for old in (
            await tx.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.incident_id == incident_id,
                    ApprovalRow.status.in_(["pending", "approved"]),
                )
            )
        ).all():
            await _expire_if_needed(tx, old, now)
        prop = inv.proposal
        runbook_id = str(prop["runbook_id"])
        params = normalize_params(runbook_id, dict(prop["params"]))
        target_revision = prop.get("target_revision")
        approval = ApprovalRow(
            incident_id=incident_id,
            investigation_id=inv.id,
            runbook_id=runbook_id,
            params=params,
            rationale=str(prop.get("rationale", ""))[:4000],
            risk=str(prop.get("risk", "unknown")),
            target_revision=target_revision,
            request_hash=compute_request_hash(incident_id, runbook_id, params, target_revision),
            status="pending",
            requested_by=principal.subject,
            requested_at=now,
            expires_at=now + APPROVAL_TTL,
        )
        tx.add(approval)
        try:
            await tx.flush()
        except IntegrityError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "an approval is already pending or approved"
            ) from None
        await _publish_approval(tx, approval, principal.subject, now)
        await audit.record(
            tx,
            actor=principal.subject,
            action="approval.requested",
            resource_type="approval",
            resource_id=str(approval.id),
            at=now,
            details={
                "incident_id": str(incident_id),
                "runbook_id": runbook_id,
                "params": params,
                "target_revision": target_revision,
            },
        )
        out = _approval_out(approval)
        idempotency.store(
            tx, principal.subject, idempotency_key, route, payload, 201, out.model_dump(mode="json")
        )
    return out


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
async def decide(
    approval_id: uuid.UUID,
    body: DecisionRequest,
    principal: ApprovalDecider,
    request: Request,
) -> ApprovalOut:
    now = datetime.now(UTC)
    async with transaction(_sessions(request)) as tx:
        a = await tx.scalar(
            select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
        )
        if a is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
        await _expire_if_needed(tx, a, now)
        if a.status != "pending":
            raise HTTPException(status.HTTP_409_CONFLICT, f"approval is {a.status}")
        if a.requested_by == principal.subject:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "four-eyes rule: you cannot decide your own request"
            )
        a.decided_at = now
        a.decision_reason = body.reason
        if body.decision == "approve":
            a.status = "approved"
            a.approved_by = principal.subject
        else:
            a.status = "rejected"
            await repo.transition(
                tx,
                a.incident_id,
                IncidentStatus.REJECTED,
                actor=principal.subject,
                reason=f"remediation rejected: {body.reason}",
                correlation_id=str(a.id),
                at=now,
            )
        await tx.flush()
        await _publish_approval(tx, a, principal.subject, now, body.reason)
        await audit.record(
            tx,
            actor=principal.subject,
            action=f"approval.{a.status}",
            resource_type="approval",
            resource_id=str(a.id),
            at=now,
            details={"incident_id": str(a.incident_id), "reason": body.reason},
        )
        return _approval_out(a)


async def _publish_approval(
    tx: AsyncSession, a: ApprovalRow, actor: str, now: datetime, reason: str | None = None
) -> None:
    await outbox.enqueue(
        tx,
        Topic.APPROVALS,
        str(a.incident_id),
        ApprovalEvent(
            event_id=f"approval:{a.id}:{a.status}",
            approval_id=a.id,
            incident_id=a.incident_id,
            runbook_id=a.runbook_id,
            params=a.params,
            status=a.status,
            actor=actor,
            reason=reason,
            occurred_at=now,
        ),
    )


@router.get("/incidents/{incident_id}/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    incident_id: uuid.UUID, session: DbSession, _: Reader
) -> list[ApprovalOut]:
    await load_incident(session, incident_id)
    rows = (
        await session.scalars(
            select(ApprovalRow)
            .where(ApprovalRow.incident_id == incident_id)
            .order_by(ApprovalRow.requested_at.desc())
        )
    ).all()
    return [_approval_out(a) for a in rows]


# ------------------------------------------------------------------ execution
@router.post(
    "/incidents/{incident_id}/actions/{runbook_id}/execute",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExecutionOut,
)
async def execute(
    incident_id: uuid.UUID,
    runbook_id: str,
    body: ExecuteRequest,
    principal: Executor,
    request: Request,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> ExecutionOut | JSONResponse:
    route = f"POST /incidents/{incident_id}/actions/{runbook_id}/execute"
    payload = body.model_dump(mode="json")
    prior = await idempotency.lookup(session, principal.subject, idempotency_key, route, payload)
    if prior:
        return JSONResponse(prior.response, status_code=prior.status_code)
    now = datetime.now(UTC)
    async with transaction(_sessions(request)) as tx:
        a = await tx.scalar(
            select(ApprovalRow).where(ApprovalRow.id == body.approval_id).with_for_update()
        )
        if a is None or a.incident_id != incident_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found for this incident")
        await _expire_if_needed(tx, a, now)
        if a.runbook_id != runbook_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "approval is for a different runbook")
        if a.status != "approved":
            raise HTTPException(status.HTTP_409_CONFLICT, f"approval is {a.status}")
        inc = await repo.get(tx, incident_id, lock=True)
        if inc.status != IncidentStatus.AWAITING_APPROVAL.value:
            raise HTTPException(status.HTTP_409_CONFLICT, f"incident is {inc.status}")
        a.status = "consumed"
        ex = ExecutionRow(
            incident_id=incident_id,
            approval_id=a.id,
            runbook_id=runbook_id,
            target=f"{a.params.get('namespace', 'sentinelops-demo')}/{a.params['deployment']}",
            params=a.params,
            status="pending",
            idempotency_key=f"{principal.subject}:{idempotency_key}",
            requested_by=principal.subject,
            policy={},
            command_template="",
            before_state={},
            after_state={},
            verification={},
        )
        tx.add(ex)
        try:
            await tx.flush()
        except IntegrityError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "another action is already in flight for this target"
            ) from None
        await repo.transition(
            tx,
            incident_id,
            IncidentStatus.REMEDIATING,
            actor=principal.subject,
            reason=f"executing {runbook_id} (approved by {a.approved_by})",
            correlation_id=str(ex.id),
            at=now,
        )
        await outbox.enqueue(
            tx,
            Topic.ACTIONS,
            str(incident_id),
            ActionEvent(
                event_id=f"action:{ex.id}:requested",
                kind=ActionEventKind.REQUESTED,
                execution_id=ex.id,
                incident_id=incident_id,
                approval_id=a.id,
                runbook_id=runbook_id,
                actor=principal.subject,
                occurred_at=now,
            ),
        )
        await audit.record(
            tx,
            actor=principal.subject,
            action="execution.requested",
            resource_type="execution",
            resource_id=str(ex.id),
            at=now,
            details={
                "incident_id": str(incident_id),
                "approval_id": str(a.id),
                "runbook_id": runbook_id,
                "params": a.params,
            },
        )
        out = _execution_out(ex)
        idempotency.store(
            tx, principal.subject, idempotency_key, route, payload, 202, out.model_dump(mode="json")
        )
    return out


@router.get("/incidents/{incident_id}/executions", response_model=list[ExecutionOut])
async def list_executions(
    incident_id: uuid.UUID, session: DbSession, _: Reader
) -> list[ExecutionOut]:
    await load_incident(session, incident_id)
    rows = (
        await session.scalars(
            select(ExecutionRow)
            .where(ExecutionRow.incident_id == incident_id)
            .order_by(ExecutionRow.created_at.desc())
        )
    ).all()
    return [_execution_out(e) for e in rows]


@router.get("/runbooks")
async def list_runbooks(_: Reader) -> list[dict[str, Any]]:
    from sentinel_policy import describe_catalog

    return describe_catalog()
