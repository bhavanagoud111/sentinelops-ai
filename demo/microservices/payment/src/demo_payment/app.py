"""Demo payment provider simulator.

Supported fault (spec section 15, scenario C):
  ``provider_slowdown`` — the (simulated third-party) provider responds slowly.
  Checkout's client timeouts and retries amplify it into checkout 5xx errors.
  Injected at runtime via the admin API: it models an external outage, not a
  rollout, so no allowlisted runbook can fix it — the correct response is escalation.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from demo_faultkit import FaultRegistry, mount_if_enabled
from fastapi import FastAPI
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field, StringConstraints

tracer = trace.get_tracer("demo_payment")
FAULT_PROVIDER_SLOWDOWN = "provider_slowdown"


class PaymentRequest(BaseModel):
    order_ref: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    amount_cents: Annotated[int, Field(ge=1, le=10_000_000)]


class PaymentResponse(BaseModel):
    payment_id: str
    status: str


def create_app(*, faults: FaultRegistry | None = None, fault_token: str | None = None) -> FastAPI:
    registry = faults or FaultRegistry([FAULT_PROVIDER_SLOWDOWN])
    meter = metrics.get_meter("demo_payment")
    payments = meter.create_counter("payment_authorizations", description="Authorizations")
    app = FastAPI(title="demo-payment", version="0.1.0")
    app.state.faults = registry
    mount_if_enabled(app.include_router, registry, fault_token)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/payments", response_model=PaymentResponse)
    async def authorize(req: PaymentRequest) -> PaymentResponse:
        spec = registry.triggered(FAULT_PROVIDER_SLOWDOWN)
        with tracer.start_as_current_span("payment.provider.authorize") as span:
            if spec and spec.delay_ms:
                span.set_attribute("demo.fault", FAULT_PROVIDER_SLOWDOWN)
                await asyncio.sleep(spec.delay_ms / 1000)
            else:
                await asyncio.sleep(0.005)
        payments.add(1)
        return PaymentResponse(payment_id=str(uuid.uuid4()), status="authorized")

    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from sentinel_common import TelemetrySettings, setup_telemetry

    handle = setup_telemetry(TelemetrySettings())
    app = create_app(fault_token=os.environ.get("FAULT_ADMIN_TOKEN"))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        handle.shutdown()

    app.router.lifespan_context = lifespan
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app
