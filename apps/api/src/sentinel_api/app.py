"""SentinelOps API application factory."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_api.auth import TokenService
from sentinel_api.events import Broadcaster
from sentinel_api.routes import auth as auth_routes
from sentinel_api.routes import experiments as experiment_routes
from sentinel_api.routes import incidents as incident_routes
from sentinel_api.routes import investigations as investigation_routes
from sentinel_api.routes import remediation as remediation_routes
from sentinel_api.routes import system as system_routes

logger = logging.getLogger("sentinel_api")


def create_app(
    *,
    sessions: async_sessionmaker[AsyncSession],
    tokens: TokenService,
    dev_login: bool = False,
    cors_origins: list[str] | None = None,
    broadcaster: Broadcaster | None = None,
    faultinjector_token: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="SentinelOps API",
        version="0.1.0",
        description="Incidents, investigations, approvals and remediation for SentinelOps AI.",
    )
    app.state.sessions = sessions
    app.state.tokens = tokens
    app.state.dev_login = dev_login
    app.state.broadcaster = broadcaster or Broadcaster()
    app.state.faultinjector_token = faultinjector_token
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_routes.router)
    app.include_router(incident_routes.router)
    app.include_router(investigation_routes.router)
    app.include_router(remediation_routes.router)
    app.include_router(system_routes.router)
    app.include_router(experiment_routes.router)
    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_common.secrets import read_secret
    from sentinel_storage import create_engine, session_factory

    handle = setup_telemetry(TelemetrySettings())
    engine = create_engine()
    origins = [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o]
    broadcaster = Broadcaster()
    app = create_app(
        sessions=session_factory(engine),
        tokens=TokenService(read_secret("JWT_SECRET", min_length=32) or ""),
        dev_login=os.environ.get("AUTH_DEV_LOGIN", "false").lower() == "true",
        cors_origins=origins,
        broadcaster=broadcaster,
        faultinjector_token=read_secret("FAULTINJECTOR_TOKEN", required=False),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        import asyncio

        from sentinel_api.events import consume

        stop = asyncio.Event()
        task = asyncio.create_task(
            consume(broadcaster, os.environ.get("KAFKA_BOOTSTRAP", "redpanda:9092"), stop)
        )
        yield
        stop.set()
        task.cancel()
        await engine.dispose()
        handle.shutdown()

    app.router.lifespan_context = lifespan
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app
