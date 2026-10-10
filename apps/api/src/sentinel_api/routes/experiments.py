"""Experiments: trigger the five sandbox fault scenarios (admin only, audited)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from sentinel_storage import audit
from sentinel_storage.db import transaction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_api.auth import Experimenter, Reader

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])
INJECTOR = os.environ.get("FAULTINJECTOR_URL", "http://faultinjector:8000")


async def _call(request: Request, method: str, path: str) -> Any:
    token: str | None = request.app.state.faultinjector_token
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "fault injector not configured")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.request(
                method, f"{INJECTOR}{path}", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "fault injector unreachable") from None
    if r.status_code == 404:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown scenario")
    if r.is_error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"fault injector error {r.status_code}")
    return r.json()


async def _audit(request: Request, actor: str, action: str, resource: str) -> None:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with transaction(sessions) as tx:
        await audit.record(
            tx,
            actor=actor,
            action=action,
            resource_type="experiment",
            resource_id=resource,
            at=datetime.now(UTC),
        )


@router.get("")
async def list_experiments(request: Request, _: Reader) -> Any:
    return await _call(request, "GET", "/scenarios")


@router.post("/reset")
async def reset(request: Request, principal: Experimenter) -> Any:
    result = await _call(request, "POST", "/reset")
    await _audit(request, principal.subject, "experiment.reset", "baseline")
    return result


@router.post("/{scenario_id}")
async def start(scenario_id: str, request: Request, principal: Experimenter) -> Any:
    if scenario_id not in {"A", "B", "C", "D", "E"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown scenario")
    result = await _call(request, "POST", f"/scenarios/{scenario_id}")
    await _audit(request, principal.subject, "experiment.started", scenario_id)
    return result
