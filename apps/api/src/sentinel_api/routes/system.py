"""Dashboard support endpoints: live stream, service metrics, system status, stats.

The metrics endpoint runs a *fixed* set of recording-rule queries per known
service — the browser never sends PromQL.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sentinel_storage.models import ExecutionRow, IncidentTransitionRow, Service
from sentinel_storage.models import Incident as IncidentRow
from sqlalchemy import func, select

from sentinel_api.auth import Reader
from sentinel_api.deps import DbSession
from sentinel_api.events import Broadcaster, stream

router = APIRouter(prefix="/api/v1", tags=["system"])

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
METRIC_QUERIES = {
    "request_rate": 'service:http_requests:rate1m{{service_name="{s}"}}',
    "error_ratio": 'service:http_errors_5xx:ratio1m{{service_name="{s}"}}',
    "latency_p95": 'service:http_latency_seconds:p95_1m{{service_name="{s}"}}',
    "memory_rss": 'max(process_resident_memory_bytes{{service_name="{s}"}})',
}
WINDOWS = {"15m": 15, "30m": 30, "1h": 60, "3h": 180}


@router.get("/events/stream")
async def events_stream(request: Request, _: Reader) -> StreamingResponse:
    broadcaster: Broadcaster = request.app.state.broadcaster
    try:
        gen = stream(broadcaster, request.is_disconnected)
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "stream at capacity") from None
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class Series(BaseModel):
    metric: str
    points: list[tuple[float, float]]


class ServiceMetrics(BaseModel):
    service: str
    window: str
    step_s: int
    series: list[Series]


@router.get("/services/{name}/metrics", response_model=ServiceMetrics)
async def service_metrics(
    name: str,
    session: DbSession,
    _: Reader,
    window: Annotated[str, Query(pattern="^(15m|30m|1h|3h)$")] = "30m",
) -> ServiceMetrics:
    known = await session.scalar(select(Service.name).where(Service.name == name))
    if known is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown service")
    minutes = WINDOWS[window]
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    step = max(15, minutes * 60 // 120)
    out: list[Series] = []
    async with httpx.AsyncClient(base_url=PROMETHEUS, timeout=5) as client:
        for metric, template in METRIC_QUERIES.items():
            try:
                r = await client.get(
                    "/api/v1/query_range",
                    params={
                        "query": template.format(s=name),
                        "start": start.timestamp(),
                        "end": end.timestamp(),
                        "step": step,
                    },
                )
                result = r.json().get("data", {}).get("result", [])
            except (httpx.HTTPError, ValueError):
                result = []
            pts: list[tuple[float, float]] = []
            for series in result[:1]:
                for ts, v in series.get("values", []):
                    fv = float(v)
                    if fv == fv and fv not in (float("inf"), float("-inf")):
                        pts.append((float(ts), fv))
            out.append(Series(metric=metric, points=pts))
    return ServiceMetrics(service=name, window=window, step_s=step, series=out)


@router.get("/stats")
async def stats(session: DbSession, _: Reader) -> dict[str, Any]:
    """Counts by status plus measured MTTR (first_seen → RESOLVED by verifier)."""
    by_status = dict(
        (
            await session.execute(
                select(IncidentRow.status, func.count()).group_by(IncidentRow.status)
            )
        ).all()
    )
    resolved_by_verifier = (
        await session.execute(
            select(IncidentRow.first_seen, IncidentTransitionRow.at)
            .join(IncidentTransitionRow, IncidentTransitionRow.incident_id == IncidentRow.id)
            .where(
                IncidentTransitionRow.to_status == "RESOLVED",
                IncidentTransitionRow.actor == "system:verifier",
            )
        )
    ).all()
    durations = sorted((at - fs).total_seconds() for fs, at in resolved_by_verifier)
    execs = dict(
        (
            await session.execute(
                select(ExecutionRow.status, func.count()).group_by(ExecutionRow.status)
            )
        ).all()
    )
    return {
        "incidents_by_status": by_status,
        "verified_recoveries": len(durations),
        "mttr_seconds_p50": durations[len(durations) // 2] if durations else None,
        "executions_by_status": execs,
    }


@router.get("/system/status")
async def system_status(request: Request, _: Reader) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    targets = {
        "prometheus": f"{PROMETHEUS}/-/ready",
        "loki": os.environ.get("LOKI_URL", "http://loki:3100") + "/ready",
        "tempo": os.environ.get("TEMPO_URL", "http://tempo:3200") + "/ready",
        "alertmanager": os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093") + "/-/ready",
    }
    async with httpx.AsyncClient(timeout=2) as client:
        for name, url in targets.items():
            try:
                checks[name] = (await client.get(url)).status_code == 200
            except httpx.HTTPError:
                checks[name] = False
    return {
        "integrations": checks,
        "remediation_mode": os.environ.get("REMEDIATION_MODE", "recommend_only"),
        "llm_provider": os.environ.get("SENTINEL_LLM_PROVIDER", "rules"),
        "grafana_url": os.environ.get("GRAFANA_PUBLIC_URL", "http://127.0.0.1:13000"),
        "stream_subscribers": request.app.state.broadcaster.subscribers,
        "dev_login": request.app.state.dev_login,
    }
