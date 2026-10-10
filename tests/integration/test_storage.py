"""Repository behaviour against a real, freshly migrated PostgreSQL database."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sentinel_contracts import IncidentStatus, InvalidTransitionError, Severity, Topic
from sentinel_storage import audit, dedup, outbox
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import Incident as IncidentRow
from sentinel_storage.models import OutboxEvent
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
Sessions = async_sessionmaker[AsyncSession]


def _incident(fp: str = "local:checkout:X", status: str = "OPEN") -> IncidentRow:
    return IncidentRow(
        fingerprint=fp,
        title="t",
        status=status,
        severity=Severity.HIGH.value,
        service_name="checkout",
        environment="local",
        alert_active=True,
        alert_count=1,
        first_seen=NOW,
        last_seen=NOW,
        summary="",
    )


async def test_migrations_seed_service_catalog(db: Sessions) -> None:
    async with db() as s:
        names = (await s.execute(text("SELECT name FROM services ORDER BY name"))).scalars().all()
    assert names == ["checkout", "inventory", "payment", "worker"]


async def test_only_one_open_incident_per_fingerprint(db: Sessions) -> None:
    async with transaction(db) as s:
        s.add(_incident())
    with pytest.raises(IntegrityError):
        async with transaction(db) as s:
            s.add(_incident())
    # A resolved incident with the same fingerprint is fine.
    async with transaction(db) as s:
        s.add(_incident(status="RESOLVED"))


async def test_transition_records_history_audit_and_outbox(db: Sessions) -> None:
    async with transaction(db) as s:
        row = _incident()
        s.add(row)
    async with transaction(db) as s:
        await repo.transition(
            s, row.id, IncidentStatus.TRIAGING, actor="u", reason="r", correlation_id="c", at=NOW
        )
    async with db() as s:
        history = await repo.timeline(s, row.id)
        out = (
            await s.scalars(select(OutboxEvent).where(OutboxEvent.topic == Topic.INCIDENTS))
        ).all()
        assert [h.to_status for h in history] == ["TRIAGING"]
        assert out[-1].payload["incident"]["status"] == "TRIAGING"
        assert await audit.verify_chain(s) is None


async def test_illegal_transition_rejected(db: Sessions) -> None:
    async with transaction(db) as s:
        row = _incident()
        s.add(row)
    with pytest.raises(InvalidTransitionError):
        async with transaction(db) as s:
            await repo.transition(
                s,
                row.id,
                IncidentStatus.REMEDIATING,
                actor="u",
                reason="r",
                correlation_id="c",
                at=NOW,
            )


async def test_optimistic_lock_rejects_stale_version(db: Sessions) -> None:
    async with transaction(db) as s:
        row = _incident()
        s.add(row)
    stale_version = row.version
    async with transaction(db) as s:
        await repo.transition(
            s,
            row.id,
            IncidentStatus.TRIAGING,
            actor="a",
            reason="r",
            correlation_id="c",
            at=NOW,
            expected_version=stale_version,
        )
    with pytest.raises(repo.StaleIncidentError):
        async with transaction(db) as s:
            await repo.transition(
                s,
                row.id,
                IncidentStatus.NEEDS_HUMAN,
                actor="b",
                reason="r",
                correlation_id="c",
                at=NOW,
                expected_version=stale_version,
            )


async def test_audit_log_is_append_only(db: Sessions) -> None:
    async with transaction(db) as s:
        await audit.record(s, actor="a", action="x", resource_type="t", resource_id="1", at=NOW)
    with pytest.raises(DBAPIError, match="append-only"):
        async with transaction(db) as s:
            await s.execute(text("UPDATE audit_events SET actor = 'mallory'"))
    with pytest.raises(DBAPIError, match="append-only"):
        async with transaction(db) as s:
            await s.execute(text("DELETE FROM audit_events"))


async def test_dedup_claim_once(db: Sessions) -> None:
    async with transaction(db) as s:
        assert await dedup.claim(s, "c1", "e1") is True
        assert await dedup.claim(s, "c1", "e1") is False
        assert await dedup.claim(s, "c2", "e1") is True


async def test_outbox_skip_locked_prevents_double_claim(db: Sessions) -> None:
    from pydantic import BaseModel

    class Msg(BaseModel):
        n: int

    async with transaction(db) as s:
        for i in range(10):
            await outbox.enqueue(s, "t", str(i), Msg(n=i))

    claimed: list[list[int]] = []
    gate = asyncio.Event()

    async def worker() -> None:
        async with transaction(db) as s:
            rows = await outbox.claim_batch(s, limit=6)
            claimed.append([r.id for r in rows])
            await gate.wait()  # hold locks while the other worker claims

    t1 = asyncio.create_task(worker())
    await asyncio.sleep(0.2)
    t2 = asyncio.create_task(worker())
    await asyncio.sleep(0.2)
    gate.set()
    await asyncio.gather(t1, t2)
    a, b = claimed
    assert set(a).isdisjoint(b)
    assert len(a) + len(b) == 10


async def test_unknown_incident_raises(db: Sessions) -> None:
    async with db() as s:
        with pytest.raises(repo.IncidentNotFoundError):
            await repo.get(s, uuid.uuid4())
