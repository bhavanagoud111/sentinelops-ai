"""Idempotency-Key support for mutating endpoints (spec section 13).

The first request with a given (principal, key) stores its response; replays
with the same body get the stored response, replays with a different body get
422. Keys are scoped per principal so users cannot read each other's responses.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException, status
from sentinel_storage.models import IdempotencyKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def request_hash(route: str, body: Any) -> str:
    return hashlib.sha256(
        json.dumps({"route": route, "body": body}, sort_keys=True, default=str).encode()
    ).hexdigest()


async def lookup(
    session: AsyncSession, principal: str, key: str | None, route: str, body: Any
) -> IdempotencyKey | None:
    if key is None:
        return None
    if not 8 <= len(key) <= 128:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Idempotency-Key must be 8-128 chars")
    row = await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.principal == principal, IdempotencyKey.key == key
        )
    )
    if row is not None and row.request_hash != request_hash(route, body):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Idempotency-Key reused with a different request",
        )
    return row


def store(
    session: AsyncSession,
    principal: str,
    key: str | None,
    route: str,
    body: Any,
    status_code: int,
    response: dict[str, Any],
) -> None:
    if key is None:
        return
    session.add(
        IdempotencyKey(
            principal=principal,
            key=key,
            route=route,
            request_hash=request_hash(route, body),
            status_code=status_code,
            response=response,
        )
    )
