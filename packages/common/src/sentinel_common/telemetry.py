"""OpenTelemetry bootstrap: traces, metrics and logs over OTLP/HTTP.

Call :func:`setup_telemetry` once at process start, before instrumenting
frameworks. When no OTLP endpoint is configured, providers are still installed
(so instrumentation works) but nothing is exported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from sentinel_common.logging import configure_logging
from sentinel_common.settings import TelemetrySettings


@dataclass(frozen=True)
class TelemetryHandle:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider

    def shutdown(self) -> None:
        """Flush and stop exporters; call on graceful shutdown."""
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        self.logger_provider.shutdown()


def setup_telemetry(settings: TelemetrySettings) -> TelemetryHandle:
    configure_logging(settings.service_name, settings.log_level)

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment.name": settings.environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    readers = []
    logger_provider = LoggerProvider(resource=resource)

    if settings.otlp_endpoint:
        base = settings.otlp_endpoint.rstrip("/")
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
        )
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
                export_interval_millis=settings.metric_export_interval_ms,
            )
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs"))
        )
        # Ship stdlib logging records to the collector in addition to stdout JSON.
        logging.getLogger().addHandler(
            LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        )

    meter_provider = MeterProvider(resource=resource, metric_readers=readers)

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    set_logger_provider(logger_provider)

    logging.getLogger(__name__).info(
        "telemetry initialised",
        extra={"otlp_endpoint": settings.otlp_endpoint or "disabled"},
    )
    return TelemetryHandle(tracer_provider, meter_provider, logger_provider)
