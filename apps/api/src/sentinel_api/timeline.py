"""Merged incident timeline: transitions, alerts (and later agent/approval/action steps)."""

from __future__ import annotations

import uuid

from sentinel_storage import incidents as repo
from sentinel_storage.models import AlertEventRow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel_api.schemas import TimelineEntry


async def build_timeline(session: AsyncSession, incident_id: uuid.UUID) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    for t in await repo.timeline(session, incident_id):
        entries.append(
            TimelineEntry(
                at=t.at,
                kind="transition",
                title=f"{t.from_status or '∅'} → {t.to_status}",
                actor=t.actor,
                details={"reason": t.reason, "correlation_id": t.correlation_id},
            )
        )
    alerts = (
        await session.scalars(
            select(AlertEventRow).where(AlertEventRow.incident_id == incident_id).limit(500)
        )
    ).all()
    for a in alerts:
        entries.append(
            TimelineEntry(
                at=a.received_at,
                kind="alert",
                title=f"{a.payload.get('alertname', 'alert')} {a.status}",
                actor=f"source:{a.payload.get('source', 'unknown')}",
                details={"event_id": a.event_id, "severity": a.payload.get("severity")},
            )
        )
    entries.sort(key=lambda e: e.at)
    return entries
