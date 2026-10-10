"""Environment-driven telemetry settings.

Standard ``OTEL_*`` variables are honoured so the services behave like any
other OpenTelemetry-instrumented workload.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = Field(validation_alias=AliasChoices("OTEL_SERVICE_NAME", "SERVICE_NAME"))
    environment: str = Field(default="local", validation_alias="DEPLOYMENT_ENVIRONMENT")
    service_version: str = Field(default="0.1.0", validation_alias="SERVICE_VERSION")
    # None disables OTLP export (unit tests, local runs without the stack).
    otlp_endpoint: str | None = Field(default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    metric_export_interval_ms: int = Field(
        default=5000, ge=1000, validation_alias="OTEL_METRIC_EXPORT_INTERVAL"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
