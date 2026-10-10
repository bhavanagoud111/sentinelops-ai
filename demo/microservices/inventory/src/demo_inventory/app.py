"""Demo inventory API.

Faults:
  * ``FEATURE_NEW_STOCK_INDEX=on`` (env, scenario B) — a "new index" code path that
    adds ``NEW_STOCK_INDEX_DELAY_MS`` latency. Shipped via a rollout, fixed by
    turning the flag off.
  * ``INVENTORY_STOCK_SEED`` other than ``default`` (env, scenario D) — invalid seed
    data makes the process exit at startup → CrashLoopBackOff after a rollout.
  * ``dependency_delay`` (admin API) — slow backing store, for Compose-only demos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from demo_faultkit import FaultRegistry, mount_if_enabled
from fastapi import FastAPI, HTTPException, status
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field, StringConstraints

logger = logging.getLogger("demo_inventory")
tracer = trace.get_tracer("demo_inventory")

FAULT_DEPENDENCY_DELAY = "dependency_delay"

Sku = Annotated[str, StringConstraints(pattern=r"^SKU-[0-9]{3}$")]

INITIAL_STOCK: dict[str, int] = {f"SKU-{i:03d}": 1_000_000 for i in range(1, 21)}


class ReserveRequest(BaseModel):
    sku: Sku
    quantity: Annotated[int, Field(ge=1, le=100)]


class ReserveResponse(BaseModel):
    sku: str
    reserved: int
    remaining: int


class StockLevel(BaseModel):
    sku: str
    available: int


class Stock:
    """In-memory stock table. Large initial values keep the demo running for days."""

    def __init__(self, initial: dict[str, int]) -> None:
        self._levels = dict(initial)
        self._lock = threading.Lock()

    def get(self, sku: str) -> int | None:
        with self._lock:
            return self._levels.get(sku)

    def reserve(self, sku: str, quantity: int) -> int:
        """Reserve stock, returning the remaining level. Raises KeyError/ValueError."""
        with self._lock:
            level = self._levels[sku]
            if level < quantity:
                raise ValueError("insufficient stock")
            self._levels[sku] = level - quantity
            return self._levels[sku]


class StartupConfigError(RuntimeError):
    pass


def validate_stock_seed(seed: str) -> None:
    if seed != "default":
        raise StartupConfigError(f"startup failed: invalid stock seed {seed!r}")


def create_app(
    *,
    faults: FaultRegistry | None = None,
    fault_token: str | None = None,
    new_stock_index: bool = False,
    new_stock_index_delay_ms: int = 1200,
) -> FastAPI:
    registry = faults or FaultRegistry([FAULT_DEPENDENCY_DELAY])
    stock = Stock(INITIAL_STOCK)
    meter = metrics.get_meter("demo_inventory")
    reservations = meter.create_counter(
        "inventory_reservations", unit="{reservation}", description="Reservation attempts"
    )

    app = FastAPI(title="demo-inventory", version="0.1.0")
    app.state.faults = registry
    app.state.stock = stock
    mount_if_enabled(app.include_router, registry, fault_token)

    async def _maybe_delay() -> None:
        if new_stock_index:
            with tracer.start_as_current_span("inventory.new_stock_index.lookup") as span:
                span.set_attribute("feature_flag.key", "FEATURE_NEW_STOCK_INDEX")
                span.set_attribute("feature_flag.variant", "on")
                await asyncio.sleep(new_stock_index_delay_ms / 1000)
        spec = registry.triggered(FAULT_DEPENDENCY_DELAY)
        if spec and spec.delay_ms:
            with tracer.start_as_current_span("inventory.store.query") as span:
                span.set_attribute("demo.fault", FAULT_DEPENDENCY_DELAY)
                span.set_attribute("demo.fault.delay_ms", spec.delay_ms)
                logger.warning(
                    "inventory store slow response",
                    extra={"fault": FAULT_DEPENDENCY_DELAY, "delay_ms": spec.delay_ms},
                )
                await asyncio.sleep(spec.delay_ms / 1000)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/inventory/{sku}", response_model=StockLevel)
    async def get_stock(sku: Sku) -> StockLevel:
        await _maybe_delay()
        level = stock.get(sku)
        if level is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown sku")
        return StockLevel(sku=sku, available=level)

    @app.post("/inventory/reserve", response_model=ReserveResponse)
    async def reserve(req: ReserveRequest) -> ReserveResponse:
        await _maybe_delay()
        try:
            remaining = stock.reserve(req.sku, req.quantity)
        except KeyError:
            reservations.add(1, {"outcome": "unknown_sku"})
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown sku") from None
        except ValueError:
            reservations.add(1, {"outcome": "insufficient"})
            raise HTTPException(status.HTTP_409_CONFLICT, "insufficient stock") from None
        reservations.add(1, {"outcome": "reserved"})
        return ReserveResponse(sku=req.sku, reserved=req.quantity, remaining=remaining)

    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    """Uvicorn factory: wires telemetry from the environment."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from sentinel_common import TelemetrySettings, setup_telemetry

    handle = setup_telemetry(TelemetrySettings())
    try:
        validate_stock_seed(os.environ.get("INVENTORY_STOCK_SEED", "default"))
    except StartupConfigError:
        logger.exception("inventory cannot start")
        handle.shutdown()  # flush the error log before exiting
        raise SystemExit(3) from None

    app = create_app(
        fault_token=os.environ.get("FAULT_ADMIN_TOKEN"),
        new_stock_index=os.environ.get("FEATURE_NEW_STOCK_INDEX", "off").lower() == "on",
        new_stock_index_delay_ms=int(os.environ.get("NEW_STOCK_INDEX_DELAY_MS", "1200")),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        handle.shutdown()

    app.router.lifespan_context = lifespan
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app
