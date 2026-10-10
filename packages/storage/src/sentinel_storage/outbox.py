"""Transactional outbox (ADR-004).

Producers call :func:`enqueue` inside the same transaction as their state change.
The relay claims unpublished rows with ``FOR UPDATE SKIP LOCKED`` so several
relay replicas can run without double-claiming a row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel_storage.models import OutboxEvent


async def enqueue(
    session: AsyncSession,
    topic: str,
    key: str,
    message: BaseModel,
    headers: dict[str, str] | None = None,
) -> OutboxEvent:
    row = OutboxEvent(
        topic=topic,
        key=key,
        payload=message.model_dump(mode="json"),
        headers=headers or {},
    )
    session.add(row)
    await session.flush()
    return row


async def claim_batch(session: AsyncSession, limit: int = 100) -> Sequence[OutboxEvent]:
    """Lock up to ``limit`` unpublished rows for this transaction, oldest first."""
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return (await session.scalars(stmt)).all()


async def mark_published(session: AsyncSession, ids: Sequence[int], at: datetime) -> None:
    if ids:
        await session.execute(
            update(OutboxEvent).where(OutboxEvent.id.in_(ids)).values(published_at=at)
        )


async def mark_failed(session: AsyncSession, row_id: int, error: str) -> None:
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == row_id)
        .values(attempts=OutboxEvent.attempts + 1, last_error=error[:2000])
    )


async def unpublished_count(session: AsyncSession) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
    return int((await session.execute(stmt)).scalar_one())


def payload_of(row: OutboxEvent) -> dict[str, Any]:
    return dict(row.payload)
