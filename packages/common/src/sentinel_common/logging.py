"""Structured JSON logging with trace correlation.

Every record carries ``trace_id``/``span_id`` from the active OpenTelemetry span
so stdout logs, Loki logs and Tempo traces can be joined.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": self._service,
            "msg": record.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Replace root handlers with a single JSON stdout handler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn's access log duplicates the OTel HTTP spans/metrics; keep it quiet.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Per-request client logs (httpx, aiokafka) are noise next to spans and metrics.
    for noisy in ("httpx", "aiokafka", "kubernetes.client.rest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
