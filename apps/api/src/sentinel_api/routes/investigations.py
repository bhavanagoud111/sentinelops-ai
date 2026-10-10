"""Investigation endpoints: trigger, list investigations, list evidence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sentinel_contracts import IncidentEventKind, IncidentStatus
from sentinel_storage import audit
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import EvidenceRow, HypothesisRow, InvestigationRow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_api import idempotency
from sentinel_api.auth import Investigator, Reader
from sentinel_api.deps import DbSession
from sentinel_api.routes.incidents import load_incident

router = APIRouter(prefix="/api/v1", tags=["investigations"])

INVESTIGABLE = {
    IncidentStatus.OPEN,
    IncidentStatus.NEEDS_HUMAN,
    IncidentStatus.REJECTED,
    IncidentStatus.FAILED,
}


class EvidenceOut(BaseModel):
    evidence_id: str
    investigation_id: uuid.UUID
    kind: str
    service: str
    source: str
    tool: str
    query: str
    range_start: datetime
    range_end: datetime
    retrieved_at: datetime
    summary: str
    anomalous: bool
    signals: dict[str, Any]
    content_hash: str
    error: str | None


class HypothesisOut(BaseModel):
    key: str
    rank: int
    cause_category: str
    suspected_service: str
    description: str
    confidence: float
    adjusted_confidence: float
    verdict: str
    critic_reasons: list[str]
    evidence_ids: list[str]
    counterevidence_ids: list[str]
    recommended_runbook_id: str | None
    runbook_params: dict[str, Any]
    uncertainty: str
    next_checks: list[str]


class InvestigationOut(BaseModel):
    id: uuid.UUID
    status: str
    provider: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    summary: str
    triage: dict[str, Any]
    steps: list[dict[str, Any]]
    proposal: dict[str, Any] | None
    usage: dict[str, Any]
    error: str | None
    hypotheses: list[HypothesisOut]


class InvestigateAccepted(BaseModel):
    incident_id: uuid.UUID
    status: str
    detail: str


@router.post(
    "/incidents/{incident_id}/investigate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InvestigateAccepted,
)
async def request_investigation(
    incident_id: uuid.UUID,
    principal: Investigator,
    session: DbSession,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> InvestigateAccepted | JSONResponse:
    route = f"POST /incidents/{incident_id}/investigate"
    prior = await idempotency.lookup(session, principal.subject, idempotency_key, route, None)
    if prior:
        return JSONResponse(prior.response, status_code=prior.status_code)
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    now = datetime.now(UTC)
    async with transaction(sessions) as tx:
        try:
            row = await repo.get(tx, incident_id, lock=True)
        except repo.IncidentNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found") from None
        current = IncidentStatus(row.status)
        if current not in INVESTIGABLE:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"incident is {current.value}; investigation allowed from "
                f"{sorted(s.value for s in INVESTIGABLE)}",
            )
        running = await tx.scalar(
            select(InvestigationRow.id).where(
                InvestigationRow.incident_id == incident_id, InvestigationRow.status == "running"
            )
        )
        if running:
            raise HTTPException(status.HTTP_409_CONFLICT, "an investigation is already running")
        await repo.publish(
            tx,
            row,
            IncidentEventKind.INVESTIGATION_REQUESTED,
            at=now,
            correlation_id=f"api:{uuid.uuid4().hex[:16]}",
        )
        await audit.record(
            tx,
            actor=principal.subject,
            action="investigation.requested",
            resource_type="incident",
            resource_id=str(incident_id),
            at=now,
        )
        body = InvestigateAccepted(
            incident_id=incident_id, status=current.value, detail="investigation queued"
        )
        idempotency.store(
            tx, principal.subject, idempotency_key, route, None, 202, body.model_dump(mode="json")
        )
    return body


@router.get("/incidents/{incident_id}/investigations", response_model=list[InvestigationOut])
async def list_investigations(
    incident_id: uuid.UUID, session: DbSession, _: Reader
) -> list[InvestigationOut]:
    await load_incident(session, incident_id)
    invs = (
        await session.scalars(
            select(InvestigationRow)
            .where(InvestigationRow.incident_id == incident_id)
            .order_by(InvestigationRow.started_at.desc())
            .limit(20)
        )
    ).all()
    hyps = (
        await session.scalars(
            select(HypothesisRow)
            .where(HypothesisRow.incident_id == incident_id)
            .order_by(HypothesisRow.rank)
        )
    ).all()
    by_inv: dict[uuid.UUID, list[HypothesisRow]] = {}
    for h in hyps:
        by_inv.setdefault(h.investigation_id, []).append(h)
    return [
        InvestigationOut(
            id=i.id,
            status=i.status,
            provider=i.provider,
            trigger=i.trigger,
            started_at=i.started_at,
            finished_at=i.finished_at,
            summary=i.summary,
            triage=i.triage,
            steps=list(i.steps),
            proposal=i.proposal,
            usage=i.usage,
            error=i.error,
            hypotheses=[
                HypothesisOut(
                    key=h.key,
                    rank=h.rank,
                    cause_category=h.cause_category,
                    suspected_service=h.suspected_service,
                    description=h.description,
                    confidence=h.confidence,
                    adjusted_confidence=h.adjusted_confidence,
                    verdict=h.verdict,
                    critic_reasons=[str(x) for x in h.critic_reasons],
                    evidence_ids=[str(x) for x in h.evidence_refs],
                    counterevidence_ids=[str(x) for x in h.counterevidence_refs],
                    recommended_runbook_id=h.recommended_runbook_id,
                    runbook_params=h.runbook_params,
                    uncertainty=h.uncertainty,
                    next_checks=[str(x) for x in h.next_checks],
                )
                for h in by_inv.get(i.id, [])
            ],
        )
        for i in invs
    ]


@router.get("/incidents/{incident_id}/evidence", response_model=list[EvidenceOut])
async def list_evidence(incident_id: uuid.UUID, session: DbSession, _: Reader) -> list[EvidenceOut]:
    await load_incident(session, incident_id)
    rows = (
        await session.scalars(
            select(EvidenceRow)
            .where(EvidenceRow.incident_id == incident_id)
            .order_by(EvidenceRow.anomalous.desc(), EvidenceRow.service, EvidenceRow.kind)
            .limit(500)
        )
    ).all()
    return [
        EvidenceOut(
            evidence_id=r.id,
            investigation_id=r.investigation_id,
            kind=r.kind,
            service=r.service,
            source=r.source,
            tool=r.tool,
            query=r.query_ref,
            range_start=r.range_start,
            range_end=r.range_end,
            retrieved_at=r.retrieved_at,
            summary=r.summary,
            anomalous=r.anomalous,
            signals=r.signals,
            content_hash=r.content_hash,
            error=r.error,
        )
        for r in rows
    ]
