"""Append-only, hash-chained audit log.

Each row stores ``hash = sha256(prev_hash || canonical_json(row))``. Editing or
deleting any historical row breaks every later hash, which :func:`verify_chain`
detects. Appends are serialized with a transaction-scoped advisory lock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel_storage.models import AuditEvent

GENESIS = "0" * 64
_LOCK_KEY = 0x5E7710E5  # arbitrary constant for pg_advisory_xact_lock


def compute_hash(
    prev_hash: str,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any],
    at: datetime,
) -> str:
    canonical = json.dumps(
        {
            "actor": actor,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": details,
            "at": at.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


async def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    at: datetime,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = await session.scalar(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1))
    prev_hash = prev or GENESIS
    data = json.loads(json.dumps(details or {}, default=str))
    row = AuditEvent(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=data,
        at=at,
        prev_hash=prev_hash,
        hash=compute_hash(
            prev_hash,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=data,
            at=at,
        ),
    )
    session.add(row)
    await session.flush()
    return row


def verify_rows(rows: Sequence[AuditEvent]) -> int | None:
    """Return the id of the first row whose hash does not verify, else ``None``."""
    prev = GENESIS
    for row in rows:
        expected = compute_hash(
            prev,
            actor=row.actor,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            details=row.details,
            at=row.at,
        )
        if row.prev_hash != prev or row.hash != expected:
            return row.id
        prev = row.hash
    return None


async def verify_chain(session: AsyncSession) -> int | None:
    rows = (await session.scalars(select(AuditEvent).order_by(AuditEvent.id))).all()
    return verify_rows(rows)
