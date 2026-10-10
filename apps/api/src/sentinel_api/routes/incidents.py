"""Incident read endpoints."""

from __future__ import annotations

import binascii
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sentinel_contracts import Incident, IncidentStatus
from sentinel_storage import incidents as repo
from sentinel_storage.models import AlertEventRow, Service
from sentinel_storage.models import Incident as IncidentRow
from sqlalchemy import select

from sentinel_api.auth import Reader
from sentinel_api.deps import DbSession
from sentinel_api.schemas import (
    AlertEventOut,
    IncidentDetail,
    Page,
    ServiceOut,
    TimelineEntry,
    decode_cursor,
    encode_cursor,
)
from sentinel_api.timeline import build_timeline

router = APIRouter(prefix="/api/v1", tags=["incidents"])


async def load_incident(session: DbSession, incident_id: uuid.UUID) -> IncidentRow:
    try:
        return await repo.get(session, incident_id)
    except repo.IncidentNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found") from None


@router.get("/incidents", response_model=Page[Incident])
async def list_incidents(
    session: DbSession,
    _: Reader,
    status_: Annotated[list[IncidentStatus] | None, Query(alias="status")] = None,
    service: Annotated[str | None, Query(max_length=128)] = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[Incident]:
    try:
        parsed = decode_cursor(cursor) if cursor else None
    except (ValueError, binascii.Error):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
    rows = await repo.list_page(
        session,
        status=[s.value for s in status_] if status_ else None,
        service=service,
        cursor=parsed,
        limit=limit + 1,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].first_seen, rows[-1].id) if more and rows else None
    return Page(items=[repo.to_contract(r) for r in rows], next_cursor=next_cursor)


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def get_incident(incident_id: uuid.UUID, session: DbSession, _: Reader) -> IncidentDetail:
    row = await load_incident(session, incident_id)
    alerts = (
        await session.scalars(
            select(AlertEventRow)
            .where(AlertEventRow.incident_id == incident_id)
            .order_by(AlertEventRow.received_at.desc())
            .limit(100)
        )
    ).all()
    svc = await session.scalar(select(Service).where(Service.name == row.service_name))
    return IncidentDetail(
        incident=repo.to_contract(row),
        alerts=[
            AlertEventOut(
                event_id=a.event_id,
                status=a.status,
                received_at=a.received_at,
                alertname=a.payload.get("alertname", ""),
                severity=a.payload.get("severity", ""),
                starts_at=a.payload["starts_at"],
                labels=a.payload.get("labels", {}),
                annotations=a.payload.get("annotations", {}),
            )
            for a in alerts
        ],
        service=_service_out(svc) if svc else None,
    )


@router.get("/incidents/{incident_id}/timeline", response_model=list[TimelineEntry])
async def get_timeline(
    incident_id: uuid.UUID, session: DbSession, _: Reader
) -> list[TimelineEntry]:
    await load_incident(session, incident_id)
    return await build_timeline(session, incident_id)


@router.get("/services", response_model=list[ServiceOut])
async def list_services(session: DbSession, _: Reader) -> list[ServiceOut]:
    rows = (await session.scalars(select(Service).order_by(Service.name))).all()
    return [_service_out(s) for s in rows]


def _service_out(s: Service) -> ServiceOut:
    return ServiceOut(
        name=s.name,
        namespace=s.namespace,
        owner=s.owner,
        criticality=s.criticality,
        description=s.description,
        dependencies=[str(d) for d in s.dependencies],
    )
