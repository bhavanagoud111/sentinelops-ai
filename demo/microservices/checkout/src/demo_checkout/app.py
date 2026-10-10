"""Demo checkout API.

``POST /checkout`` validates payment configuration, reserves stock in the
inventory service, authorizes payment (with timeout + retries) and returns an order.

Faults:
  * ``PAYMENT_PROVIDER_URL`` set to a malformed value (env, scenario A) — a bad
    configuration rollout: every request fails with 500 and logs a ConfigError.
  * slow payment provider (scenario C) — client timeouts and retries amplify it into
    504s; checkout is the victim.
  * ``bad_config`` (admin API) — the same config failure at runtime, Compose demos only.
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
        default="http://payment:8000/v1", validation_alias="PAYMENT_PROVIDER_URL"
    )
    inventory_timeout_s: float = Field(default=2.0, gt=0, validation_alias="INVENTORY_TIMEOUT_S")
    payment_timeout_s: float = Field(default=0.8, gt=0, validation_alias="PAYMENT_TIMEOUT_S")
    payment_attempts: int = Field(default=3, ge=1, le=5, validation_alias="PAYMENT_ATTEMPTS")
    # Compose-only demo stacks have no payment service; skip the call there.
    payment_enabled: bool = Field(default=True, validation_alias="PAYMENT_ENABLED")


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
    dep_errors = meter.create_counter(
        "checkout_dependency_errors", description="Failed outbound calls by dependency and reason"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = http_client is None
        client = http_client or httpx.AsyncClient(timeout=cfg.inventory_timeout_s)
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
        provider = _payment_provider()
        client: httpx.AsyncClient = request.app.state.http
        try:
            resp = await client.post(
                f"{cfg.inventory_url}/inventory/reserve",
                json={"sku": req.sku, "quantity": req.quantity},
            )
        except httpx.TimeoutException:
            dep_errors.add(1, {"dependency": "inventory", "reason": "timeout"})
            orders.add(1, {"outcome": "inventory_timeout"})
            logger.error("inventory reservation timed out", extra={"sku": req.sku})
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "inventory timeout") from None
        except httpx.HTTPError as exc:
            dep_errors.add(1, {"dependency": "inventory", "reason": "unreachable"})
            orders.add(1, {"outcome": "inventory_unreachable"})
            logger.error("inventory unreachable", extra={"sku": req.sku, "error": str(exc)})
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "inventory unavailable") from None

        if resp.status_code == status.HTTP_409_CONFLICT:
            orders.add(1, {"outcome": "out_of_stock"})
            raise HTTPException(status.HTTP_409_CONFLICT, "out of stock")
        if resp.is_error:
            dep_errors.add(1, {"dependency": "inventory", "reason": "error"})
            orders.add(1, {"outcome": "inventory_error"})
            logger.error(
                "inventory returned error",
                extra={"sku": req.sku, "inventory_status": resp.status_code},
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "inventory error")

        order_id = str(uuid.uuid4())
        if cfg.payment_enabled:
            await _authorize_payment(client, provider, order_id, req)
        orders.add(1, {"outcome": "placed"})
        return Order(order_id=order_id, sku=req.sku, quantity=req.quantity, status="placed")

    async def _authorize_payment(
        client: httpx.AsyncClient, provider: str, order_id: str, req: CheckoutRequest
    ) -> None:
        body = {"order_ref": order_id[:64], "amount_cents": 1999 * req.quantity}
        for attempt in range(1, cfg.payment_attempts + 1):
            try:
                resp = await client.post(
                    f"{provider.rstrip('/')}/payments", json=body, timeout=cfg.payment_timeout_s
                )
            except httpx.TimeoutException:
                dep_errors.add(1, {"dependency": "payment", "reason": "timeout"})
                logger.warning("payment attempt timed out", extra={"attempt": attempt})
                continue
            except httpx.HTTPError as exc:
                dep_errors.add(1, {"dependency": "payment", "reason": "unreachable"})
                orders.add(1, {"outcome": "payment_unreachable"})
                logger.error("payment provider unreachable", extra={"error": str(exc)})
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment unavailable") from None
            if resp.is_error:
                dep_errors.add(1, {"dependency": "payment", "reason": "error"})
                orders.add(1, {"outcome": "payment_error"})
                logger.error("payment provider error", extra={"status": resp.status_code})
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment error")
            return
        orders.add(1, {"outcome": "payment_timeout"})
        logger.error(
            "payment request timed out after %d attempts",
            cfg.payment_attempts,
            extra={"attempts": cfg.payment_attempts},
        )
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "payment timeout")

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
