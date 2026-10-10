"""Demo background worker.

Processes synthetic jobs on a loop and exports its resident memory as the OTel
gauge ``process.resident_memory`` (Prometheus: ``process_resident_memory_bytes``).

Supported fault (spec section 15, scenario E):
  ``memory_leak`` — every job retains ``delay_ms`` KiB (the spec field is reused as
  a size) in a module-level cache that is never freed. Memory grows until the pod
  is restarted; a restart clears it (mitigation), which the verifier confirms.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from demo_faultkit import FaultRegistry, mount_if_enabled
from fastapi import FastAPI
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

logger = logging.getLogger("demo_worker")
FAULT_MEMORY_LEAK = "memory_leak"
_LEAK: list[bytes] = []  # the "bug"


def rss_bytes() -> int:
    """Current resident set size (Linux /proc; falls back to peak RSS elsewhere)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)


def create_app(
    *, faults: FaultRegistry | None = None, fault_token: str | None = None, interval_s: float = 0.2
) -> FastAPI:
    registry = faults or FaultRegistry([FAULT_MEMORY_LEAK])
    meter = metrics.get_meter("demo_worker")
    jobs = meter.create_counter("worker_jobs_processed", description="Jobs processed")

    def _rss(_: CallbackOptions) -> list[Observation]:
        return [Observation(rss_bytes())]

    meter.create_observable_gauge(
        "process.resident_memory", callbacks=[_rss], unit="By", description="Resident memory"
    )

    async def loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            spec = registry.get(FAULT_MEMORY_LEAK)
            payload = sum(i * i for i in range(2000))  # simulated work
            if spec:
                _LEAK.append(b"x" * (max(spec.delay_ms, 1) * 1024))
            jobs.add(1, {"result": "ok" if payload else "empty"})
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(loop(stop))
        yield
        stop.set()
        await task

    app = FastAPI(title="demo-worker", version="0.1.0", lifespan=lifespan)
    app.state.faults = registry
    mount_if_enabled(app.include_router, registry, fault_token)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "rss_bytes": rss_bytes(), "leaked_chunks": len(_LEAK)}

    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    from sentinel_common import TelemetrySettings, setup_telemetry

    handle = setup_telemetry(TelemetrySettings())
    app = create_app(fault_token=os.environ.get("FAULT_ADMIN_TOKEN"))
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[None]:
        async with inner(a):
            yield
        handle.shutdown()

    app.router.lifespan_context = lifespan
    return app
