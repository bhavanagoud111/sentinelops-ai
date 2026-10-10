"""Incident repository: queries, state transitions and lifecycle events.

Every status change goes through :func:`transition`, which enforces the contract
state machine, bumps the optimistic-lock version, records an
``incident_transitions`` row and an audit event, and enqueues an
``IncidentEvent`` to the outbox — all in the caller's transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sentinel_contracts import (
    Incident,
    IncidentEvent,
    IncidentEventKind,
    IncidentStatus,
    Severity,
    Topic,
    require_transition,
)
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel_storage import audit, outbox
from sentinel_storage.models import Incident as IncidentRow
from sentinel_storage.models import IncidentTransitionRow


class IncidentNotFoundError(LookupError):
    pass


class StaleIncidentError(RuntimeError):
    """The incident changed since the caller read it (optimistic lock)."""


def to_contract(row: IncidentRow) -> Incident:
    return Incident(
        id=row.id,
        fingerprint=row.fingerprint,
        title=row.title,
        status=IncidentStatus(row.status),
        severity=Severity(row.severity),
        service=row.service_name,
        environment=row.environment,
        alert_active=row.alert_active,
        alert_count=row.alert_count,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        resolved_at=row.resolved_at,
        summary=row.summary,
        version=row.version,
    )


async def get(session: AsyncSession, incident_id: uuid.UUID, *, lock: bool = False) -> IncidentRow:
    stmt = select(IncidentRow).where(IncidentRow.id == incident_id)
    if lock:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    if row is None:
        raise IncidentNotFoundError(str(incident_id))
    return row


async def find_open(session: AsyncSession, fingerprint: str) -> IncidentRow | None:
    stmt = (
        select(IncidentRow)
        .where(IncidentRow.fingerprint == fingerprint, IncidentRow.status != "RESOLVED")
        .with_for_update()
    )
    return await session.scalar(stmt)


async def find_latest_resolved(session: AsyncSession, fingerprint: str) -> IncidentRow | None:
    stmt = (
        select(IncidentRow)
        .where(IncidentRow.fingerprint == fingerprint, IncidentRow.status == "RESOLVED")
        .order_by(IncidentRow.resolved_at.desc())
        .limit(1)
        .with_for_update()
    )
    return await session.scalar(stmt)


async def list_page(
    session: AsyncSession,
    *,
    status: Sequence[str] | None = None,
    service: str | None = None,
    cursor: tuple[datetime, uuid.UUID] | None = None,
    limit: int = 50,
) -> list[IncidentRow]:
    """Newest first, keyset-paginated on (first_seen, id)."""
    stmt = select(IncidentRow)
    if status:
        stmt = stmt.where(IncidentRow.status.in_(list(status)))
    if service:
        stmt = stmt.where(IncidentRow.service_name == service)
    if cursor:
        ts, last_id = cursor
        stmt = stmt.where(
            or_(
                IncidentRow.first_seen < ts,
                and_(IncidentRow.first_seen == ts, IncidentRow.id < last_id),
            )
        )
    stmt = stmt.order_by(IncidentRow.first_seen.desc(), IncidentRow.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def record_transition(
    session: AsyncSession,
    row: IncidentRow,
    *,
    from_status: IncidentStatus | None,
    actor: str,
    reason: str,
    correlation_id: str,
    at: datetime,
) -> None:
    session.add(
        IncidentTransitionRow(
            incident_id=row.id,
            from_status=from_status.value if from_status else None,
            to_status=row.status,
            actor=actor,
            reason=reason,
            correlation_id=correlation_id,
            at=at,
        )
    )
    await audit.record(
        session,
        actor=actor,
        action="incident.transition",
        resource_type="incident",
        resource_id=str(row.id),
        at=at,
        details={
            "from": from_status.value if from_status else None,
            "to": row.status,
            "reason": reason,
            "correlation_id": correlation_id,
        },
    )


async def publish(
    session: AsyncSession,
    row: IncidentRow,
    kind: IncidentEventKind,
    *,
    at: datetime,
    correlation_id: str,
    previous_status: IncidentStatus | None = None,
    cause_event_id: str | None = None,
) -> IncidentEvent:
    await session.flush()  # make sure version/defaults are current
    event = IncidentEvent.build(
        kind,
        to_contract(row),
        occurred_at=at,
        correlation_id=correlation_id,
        previous_status=previous_status,
        cause_event_id=cause_event_id,
    )
    await outbox.enqueue(session, Topic.INCIDENTS, str(row.id), event)
    return event


async def transition(
    session: AsyncSession,
    incident_id: uuid.UUID,
    target: IncidentStatus,
    *,
    actor: str,
    reason: str,
    correlation_id: str,
    at: datetime,
    expected_version: int | None = None,
) -> IncidentRow:
    """Apply a legal status change, or raise InvalidTransitionError / StaleIncidentError."""
    row = await get(session, incident_id, lock=True)
    if expected_version is not None and row.version != expected_version:
        raise StaleIncidentError(
            f"incident {incident_id} is at version {row.version}, expected {expected_version}"
        )
    current = IncidentStatus(row.status)
    require_transition(current, target)
    row.status = target.value
    if target is IncidentStatus.RESOLVED:
        row.resolved_at = at
    elif current is IncidentStatus.RESOLVED:
        row.resolved_at = None
    await session.flush()
    await record_transition(
        session,
        row,
        from_status=current,
        actor=actor,
        reason=reason,
        correlation_id=correlation_id,
        at=at,
    )
    await publish(
        session,
        row,
        IncidentEventKind.STATUS_CHANGED,
        at=at,
        correlation_id=correlation_id,
        previous_status=current,
    )
    return row


async def timeline(session: AsyncSession, incident_id: uuid.UUID) -> list[IncidentTransitionRow]:
    stmt = (
        select(IncidentTransitionRow)
        .where(IncidentTransitionRow.incident_id == incident_id)
        .order_by(IncidentTransitionRow.at, IncidentTransitionRow.id)
    )
    return list((await session.scalars(stmt)).all())
