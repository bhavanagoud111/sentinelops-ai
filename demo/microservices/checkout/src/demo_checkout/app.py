"""Demo checkout API.

``POST /checkout`` validates payment configuration, reserves stock in the
inventory service and returns an order.

Supported fault (spec section 15, scenario A):
  ``bad_config`` — simulates a bad configuration rollout: the payment provider
  setting resolves to a malformed value, so affected requests fail with 5xx and
  log a ``ConfigError``. This drives the ``CheckoutHigh5xxRate`` alert.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from demo_faultkit import FaultRegistry, mount_if_enabled
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("demo_checkout")
tracer = trace.get_tracer("demo_checkout")

FAULT_BAD_CONFIG = "bad_config"
# What a broken rollout would have shipped: missing scheme and host.
BROKEN_PAYMENT_PROVIDER_URL = "payments.internal:::"


class CheckoutSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    inventory_url: str = Field(default="http://inventory:8000", validation_alias="INVENTORY_URL")
    payment_provider_url: str = Field(
        default="http://payments.internal/v1", validation_alias="PAYMENT_PROVIDER_URL"
    )
    inventory_timeout_s: float = Field(default=2.0, gt=0, validation_alias="INVENTORY_TIMEOUT_S")


class ConfigError(RuntimeError):
    """Raised when runtime configuration is unusable."""


class CheckoutRequest(BaseModel):
    sku: Annotated[str, StringConstraints(pattern=r"^SKU-[0-9]{3}$")]
    quantity: Annotated[int, Field(ge=1, le=100)] = 1


class Order(BaseModel):
    order_id: str
    sku: str
    quantity: int
    status: str


def validate_payment_provider(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"invalid payment provider url {url!r}")
    return url


def create_app(
    settings: CheckoutSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    faults: FaultRegistry | None = None,
    fault_token: str | None = None,
) -> FastAPI:
    cfg = settings or CheckoutSettings()
    registry = faults or FaultRegistry([FAULT_BAD_CONFIG])
    meter = metrics.get_meter("demo_checkout")
    orders = meter.create_counter(
        "checkout_orders", unit="{order}", description="Checkout attempts by outcome"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = http_client is None
        client = http_client or httpx.AsyncClient(
            base_url=cfg.inventory_url, timeout=cfg.inventory_timeout_s
        )
        app.state.http = client
        try:
            yield
        finally:
            if owned:
                await client.aclose()

    app = FastAPI(title="demo-checkout", version="0.1.0", lifespan=lifespan)
    app.state.faults = registry
    mount_if_enabled(app.include_router, registry, fault_token)

    @app.exception_handler(ConfigError)
    async def config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
        logger.error("checkout failed: configuration error", exc_info=exc)
        orders.add(1, {"outcome": "config_error"})
        return JSONResponse(
            {"detail": "checkout temporarily unavailable"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    def _payment_provider() -> str:
        url = (
            BROKEN_PAYMENT_PROVIDER_URL
            if registry.triggered(FAULT_BAD_CONFIG)
            else cfg.payment_provider_url
        )
        with tracer.start_as_current_span("checkout.load_payment_config") as span:
            span.set_attribute("checkout.payment_provider_url", url)
            return validate_payment_provider(url)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/checkout", response_model=Order, status_code=status.HTTP_201_CREATED)
    async def checkout(req: CheckoutRequest, request: Request) -> Order:
        _payment_provider()
        client: httpx.AsyncClient = request.app.state.http
        try:
            resp = await client.post(
                "/inventory/reserve", json={"sku": req.sku, "quantity": req.quantity}
            )
        except httpx.TimeoutException:
            orders.add(1, {"outcome": "inventory_timeout"})
            logger.error("inventory reservation timed out", extra={"sku": req.sku})
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "inventory timeout") from None
        except httpx.HTTPError as exc:
            orders.add(1, {"outcome": "inventory_unreachable"})
            logger.error("inventory unreachable", extra={"sku": req.sku, "error": str(exc)})
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "inventory unavailable") from None

        if resp.status_code == status.HTTP_409_CONFLICT:
            orders.add(1, {"outcome": "out_of_stock"})
            raise HTTPException(status.HTTP_409_CONFLICT, "out of stock")
        if resp.is_error:
            orders.add(1, {"outcome": "inventory_error"})
            logger.error(
                "inventory returned error",
                extra={"sku": req.sku, "inventory_status": resp.status_code},
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "inventory error")

        orders.add(1, {"outcome": "placed"})
        return Order(
            order_id=str(uuid.uuid4()), sku=req.sku, quantity=req.quantity, status="placed"
        )

    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    """Uvicorn factory: wires telemetry from the environment."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from sentinel_common import TelemetrySettings, setup_telemetry

    handle = setup_telemetry(TelemetrySettings())
    HTTPXClientInstrumentor().instrument()

    app = create_app(fault_token=os.environ.get("FAULT_ADMIN_TOKEN"))
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[None]:
        async with inner(a):
            yield
        handle.shutdown()

    app.router.lifespan_context = lifespan
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app
