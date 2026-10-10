"""Read-only telemetry clients used by investigation tools.

Every client is GET-only against query APIs, enforces a request timeout and a
response-size cap, and sits behind a circuit breaker so a struggling backend
degrades an investigation instead of hanging it (spec section 16).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelemetrySourcesSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    prometheus_url: str = Field(default="http://prometheus:9090", validation_alias="PROMETHEUS_URL")
    loki_url: str = Field(default="http://loki:3100", validation_alias="LOKI_URL")
    tempo_url: str = Field(default="http://tempo:3200", validation_alias="TEMPO_URL")
    timeout_s: float = Field(default=8.0, gt=0, le=60, validation_alias="TELEMETRY_TIMEOUT_S")


class TelemetryError(RuntimeError):
    pass


class CircuitOpenError(TelemetryError):
    pass


@dataclass
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens after ``reset_s``."""

    threshold: int = 3
    reset_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = 0
    _opened_at: float | None = field(default=None)

    def before(self) -> None:
        if self._opened_at is None:
            return
        if self.clock() - self._opened_at >= self.reset_s:
            self._opened_at = None  # half-open: allow one trial
            self._failures = self.threshold - 1
            return
        raise CircuitOpenError("circuit open")

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = self.clock()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


MAX_RESPONSE_BYTES = 2_000_000


class _Base:
    def __init__(
        self, base_url: str, timeout_s: float, client: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self.breaker = CircuitBreaker()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.breaker.before()
        try:
            resp = await self._client.get(path, params=params)
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise TelemetryError(f"response too large ({len(resp.content)} bytes)")
            if resp.status_code >= 500:
                raise TelemetryError(f"backend error {resp.status_code}")
            if resp.status_code >= 400:
                # Client errors (bad query) are the caller's fault: don't trip the breaker.
                self.breaker.success()
                raise TelemetryError(f"query rejected ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.breaker.failure()
            raise TelemetryError(f"{type(exc).__name__}: {exc}") from exc
        except TelemetryError as exc:
            if "rejected" not in str(exc):
                self.breaker.failure()
            raise
        self.breaker.success()
        if not isinstance(data, dict):
            raise TelemetryError("unexpected response shape")
        return data

    async def aclose(self) -> None:
        await self._client.aclose()


class PrometheusClient(_Base):
    async def query_range(
        self, promql: str, start: datetime, end: datetime, step_s: int
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/v1/query_range",
            {"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
        )
        if data.get("status") != "success":
            raise TelemetryError(str(data.get("error", "query failed")))
        return list(data["data"]["result"])


class LokiClient(_Base):
    async def query_range(
        self, logql: str, start: datetime, end: datetime, limit: int
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "/loki/api/v1/query_range",
            {
                "query": logql,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": "backward",
            },
        )
        return list(data.get("data", {}).get("result", []))


class TempoClient(_Base):
    async def search(
        self, traceql: str, start: datetime, end: datetime, limit: int
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/search",
            {
                "q": traceql,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "limit": limit,
            },
        )
        return list(data.get("traces", []))

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return await self._get(f"/api/traces/{trace_id}", {})


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "LokiClient",
    "PrometheusClient",
    "TelemetryError",
    "TelemetrySourcesSettings",
    "TempoClient",
]
