from __future__ import annotations

import json
import logging

from opentelemetry.sdk.trace import TracerProvider
from sentinel_common.logging import JsonFormatter


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_fields_and_extras() -> None:
    out = json.loads(JsonFormatter("svc").format(_record("hello", sku="SKU-001")))
    assert out["msg"] == "hello"
    assert out["service"] == "svc"
    assert out["level"] == "info"
    assert out["sku"] == "SKU-001"
    assert "trace_id" not in out


def test_trace_correlation() -> None:
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        out = json.loads(JsonFormatter("svc").format(_record("in span")))
    ctx = span.get_span_context()
    assert out["trace_id"] == format(ctx.trace_id, "032x")
    assert out["span_id"] == format(ctx.span_id, "016x")
