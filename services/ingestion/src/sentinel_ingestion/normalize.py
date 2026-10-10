"""Alertmanager webhook (v4) → AlertEvent normalization. Pure functions only."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sentinel_contracts import AlertEvent, AlertSource, AlertStatus, Severity

MAX_ALERTS_PER_WEBHOOK = 500
MAX_LABELS = 64
MAX_ANNOTATIONS = 32
MAX_VALUE_LEN = 1024
_ZERO_TIME_YEAR = 1  # Alertmanager encodes "no end" as 0001-01-01T00:00:00Z

_SEVERITY_ALIASES = {
    "critical": Severity.CRITICAL,
    "page": Severity.CRITICAL,
    "high": Severity.HIGH,
    "error": Severity.HIGH,
    "warning": Severity.WARNING,
    "warn": Severity.WARNING,
    "info": Severity.INFO,
    "none": Severity.INFO,
}


class AMAlert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: AlertStatus
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: AwareDatetime
    endsAt: AwareDatetime | None = None
    generatorURL: str = ""
    fingerprint: Annotated[str, Field(min_length=1, max_length=64)]


class AMWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str
    status: AlertStatus
    receiver: str = ""
    groupKey: str = ""
    alerts: Annotated[list[AMAlert], Field(max_length=MAX_ALERTS_PER_WEBHOOK)]


class NormalizationError(ValueError):
    pass


def _bounded(mapping: dict[str, str], limit: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in sorted(mapping)[:limit]:
        out[key[:128]] = mapping[key][:MAX_VALUE_LEN]
    return out


def _severity(labels: dict[str, str]) -> Severity:
    return _SEVERITY_ALIASES.get(labels.get("severity", "").lower(), Severity.WARNING)


def _identifier(value: str) -> str:
    """Coerce a label value into the contract identifier alphabet."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._:/-" else "-" for ch in value.strip())
    return cleaned[:128] or "unknown"


def normalize_alert(
    alert: AMAlert, *, default_environment: str, received_at: datetime
) -> AlertEvent:
    labels = alert.labels
    alertname = labels.get("alertname")
    if not alertname:
        raise NormalizationError("alert has no alertname label")
    service = labels.get("service") or labels.get("service_name") or labels.get("job")
    if not service:
        raise NormalizationError(f"alert {alertname!r} has no service/service_name/job label")
    environment = _identifier(labels.get("environment", default_environment))
    service = _identifier(service)
    alertname = _identifier(alertname)
    ends_at = alert.endsAt if alert.endsAt and alert.endsAt.year > _ZERO_TIME_YEAR else None
    bounded_labels = _bounded(labels, MAX_LABELS)
    bounded_labels["am_fingerprint"] = alert.fingerprint
    if alert.generatorURL:
        bounded_labels.setdefault("generator_url", alert.generatorURL[:MAX_VALUE_LEN])
    return AlertEvent(
        event_id=AlertEvent.derive_event_id(
            AlertSource.ALERTMANAGER, alert.fingerprint, alert.startsAt, alert.status
        ),
        source=AlertSource.ALERTMANAGER,
        status=alert.status,
        alertname=alertname,
        fingerprint=AlertEvent.derive_fingerprint(environment, service, alertname),
        service=service,
        environment=environment,
        severity=_severity(labels),
        starts_at=alert.startsAt,
        ends_at=ends_at,
        labels=dict(sorted(bounded_labels.items())[:MAX_LABELS]),
        annotations=_bounded(alert.annotations, MAX_ANNOTATIONS),
        received_at=received_at,
    )


def normalize_webhook(
    payload: dict[str, Any], *, default_environment: str, received_at: datetime
) -> tuple[list[AlertEvent], list[str]]:
    """Return (events, rejection reasons). Invalid individual alerts are skipped, not fatal."""
    hook = AMWebhook.model_validate(payload)
    if not hook.version.startswith("4"):
        raise NormalizationError(f"unsupported Alertmanager webhook version {hook.version!r}")
    events: list[AlertEvent] = []
    rejected: list[str] = []
    for alert in hook.alerts:
        try:
            events.append(
                normalize_alert(
                    alert, default_environment=default_environment, received_at=received_at
                )
            )
        except (NormalizationError, ValueError) as exc:
            rejected.append(str(exc)[:300])
    return events, rejected
