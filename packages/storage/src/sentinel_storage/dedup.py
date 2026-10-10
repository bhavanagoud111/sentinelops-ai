"""Consumer-side idempotency.

At-least-once delivery means any message can arrive twice (redelivery after a
crash, consumer-group rebalance, manual replay). Consumers call :func:`claim`
inside the transaction that applies the message's effects: if the claim
returns ``False`` the message was already applied and must be skipped.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel_storage.models import ProcessedEvent


async def claim(session: AsyncSession, consumer: str, event_id: str) -> bool:
    stmt = (
        insert(ProcessedEvent)
        .values(consumer=consumer, event_id=event_id)
        .on_conflict_do_nothing(index_elements=["consumer", "event_id"])
        .returning(ProcessedEvent.event_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
