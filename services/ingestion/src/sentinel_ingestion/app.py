"""Alert ingestion gateway (design spec section 7).

POST /api/v1/ingest/alertmanager
  1. authenticate (bearer token, constant-time compare)
  2. rate-limit per client, cap body size
  3. validate + normalize into AlertEvent(s)
  4. in ONE transaction: insert alert_events (unique event_id → duplicates are
     no-ops) and an outbox row per new event
  5. 202 with accepted/duplicate counts

Publishing to Kafka happens asynchronously in the relay (ADR-004).
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from opentelemetry import metrics
from pydantic import BaseModel, ValidationError
from sentinel_common.ratelimit import TokenBucketLimiter
from sentinel_contracts import AlertEvent, Topic
from sentinel_ingestion.normalize import NormalizationError, normalize_webhook
from sentinel_storage import outbox
from sentinel_storage.db import transaction
from sentinel_storage.models import AlertEventRow
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("sentinel_ingestion")
meter = metrics.get_meter("sentinel_ingestion")
_alerts = meter.create_counter("sentinel_ingest_alerts", description="Alerts by outcome")
_requests = meter.create_counter("sentinel_ingest_requests", description="Webhooks by outcome")

MAX_BODY_BYTES = 1_000_000


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    event_ids: list[str]


async def persist(session: AsyncSession, events: list[AlertEvent]) -> tuple[list[str], int]:
    """Insert new events + outbox rows; return (new event ids, duplicate count)."""
    new_ids: list[str] = []
    duplicates = 0
    for event in events:
        stmt = (
            insert(AlertEventRow)
            .values(
                event_id=event.event_id,
                fingerprint=event.fingerprint,
                status=event.status.value,
                payload=event.model_dump(mode="json"),
                received_at=event.received_at,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(AlertEventRow.id)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is None:
            duplicates += 1
            continue
        await outbox.enqueue(
            session, Topic.ALERTS, event.fingerprint, event, headers={"event_id": event.event_id}
        )
        new_ids.append(event.event_id)
    return new_ids, duplicates


def create_app(
    *,
    sessions: async_sessionmaker[AsyncSession],
    ingest_token: str,
    default_environment: str = "local",
    limiter: TokenBucketLimiter | None = None,
) -> FastAPI:
    if len(ingest_token) < 16:
        raise ValueError("ingest token must be at least 16 characters")
    rate_limiter = limiter or TokenBucketLimiter(rate_per_s=20, burst=100)
    app = FastAPI(title="sentinelops-ingestion", version="0.1.0")

    def authenticate(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, credentials = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credentials.encode(), ingest_token.encode()
        ):
            _requests.add(1, {"outcome": "unauthorized"})
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "invalid ingest credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/v1/ingest/alertmanager",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=IngestResult,
        dependencies=[Depends(authenticate)],
    )
    async def ingest_alertmanager(request: Request) -> IngestResult | JSONResponse:
        client = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client):
            _requests.add(1, {"outcome": "rate_limited"})
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": "1"},
            )
        declared = int(request.headers.get("content-length") or 0)
        if declared > MAX_BODY_BYTES:
            _requests.add(1, {"outcome": "too_large"})
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "payload too large")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            _requests.add(1, {"outcome": "too_large"})
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "payload too large")

        try:
            payload: Any = await request.json()
            if not isinstance(payload, dict):
                raise NormalizationError("payload must be a JSON object")
            events, rejected = normalize_webhook(
                payload,
                default_environment=default_environment,
                received_at=datetime.now(UTC),
            )
        except (ValidationError, NormalizationError, ValueError) as exc:
            _requests.add(1, {"outcome": "invalid"})
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid webhook: {str(exc)[:500]}"
            ) from None

        async with transaction(sessions) as session:
            new_ids, duplicates = await persist(session, events)

        _alerts.add(len(new_ids), {"outcome": "accepted"})
        _alerts.add(duplicates, {"outcome": "duplicate"})
        _alerts.add(len(rejected), {"outcome": "rejected"})
        _requests.add(1, {"outcome": "ok"})
        if rejected:
            logger.warning("alerts rejected", extra={"rejected": rejected[:10]})
        logger.info(
            "webhook ingested",
            extra={"accepted": len(new_ids), "duplicates": duplicates, "rejected": len(rejected)},
        )
        return IngestResult(
            accepted=len(new_ids),
            duplicates=duplicates,
            rejected=len(rejected),
            event_ids=new_ids,
        )

    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_common.secrets import read_secret
    from sentinel_storage import create_engine, session_factory

    handle = setup_telemetry(TelemetrySettings())
    engine = create_engine()
    app = create_app(
        sessions=session_factory(engine),
        ingest_token=read_secret("INGEST_TOKEN") or "",
        default_environment=os.environ.get("DEPLOYMENT_ENVIRONMENT", "local"),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()
        handle.shutdown()

    app.router.lifespan_context = lifespan
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app
