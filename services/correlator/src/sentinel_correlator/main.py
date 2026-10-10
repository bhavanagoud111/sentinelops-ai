"""Correlator: consumes ``sentinel.alerts.v1`` and maintains incidents.

Each message is applied in one transaction together with its idempotency claim,
so redelivery or replay never creates duplicate incidents. The partial unique
index on open fingerprints is the second line of defence against races.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta
from typing import Any

from aiokafka import ConsumerRecord
from opentelemetry import metrics
from pydantic import ValidationError
from sentinel_contracts import AlertEvent, IncidentEventKind, IncidentStatus, Topic
from sentinel_correlator.decide import DEFAULT_REOPEN_WINDOW, Decision, IncidentRef, decide
from sentinel_messaging import BrokerSettings, PermanentError, Producer, ensure_topics, run_consumer
from sentinel_storage import dedup
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import AlertEventRow, Service
from sentinel_storage.models import Incident as IncidentRow
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("sentinel_correlator")
meter = metrics.get_meter("sentinel_correlator")
_decisions = meter.create_counter("sentinel_correlator_decisions", description="By decision")

CONSUMER = "correlator"
ACTOR = "system:correlator"


def _ref(row: IncidentRow | None) -> IncidentRef | None:
    return IncidentRef(status=row.status, resolved_at=row.resolved_at) if row else None


async def apply_event(
    session: AsyncSession,
    event: AlertEvent,
    *,
    reopen_window: timedelta = DEFAULT_REOPEN_WINDOW,
    now: datetime | None = None,
) -> tuple[Decision, IncidentRow | None]:
    """Apply one alert event. Caller owns the transaction."""
    at = now or datetime.now(UTC)
    if not await dedup.claim(session, CONSUMER, event.event_id):
        _decisions.add(1, {"decision": "duplicate"})
        logger.info("duplicate event skipped", extra={"event_id": event.event_id})
        return Decision.IGNORE, None

    open_row = await repo.find_open(session, event.fingerprint)
    resolved_row = None if open_row else await repo.find_latest_resolved(session, event.fingerprint)
    decision = decide(event, _ref(open_row), _ref(resolved_row), reopen_window)
    _decisions.add(1, {"decision": decision.value})
    corr = event.event_id
    row: IncidentRow | None = None

    if decision is Decision.CREATE:
        service_id = await session.scalar(select(Service.id).where(Service.name == event.service))
        row = IncidentRow(
            fingerprint=event.fingerprint,
            title=event.annotations.get("summary") or event.alertname,
            status=IncidentStatus.OPEN.value,
            severity=event.severity.value,
            service_id=service_id,
            service_name=event.service,
            environment=event.environment,
            alert_active=True,
            alert_count=1,
            first_seen=event.starts_at,
            last_seen=event.starts_at,
            summary=event.annotations.get("description", "")[:4000],
        )
        session.add(row)
        await session.flush()
        await repo.record_transition(
            session,
            row,
            from_status=None,
            actor=ACTOR,
            reason=f"alert {event.alertname} firing",
            correlation_id=corr,
            at=at,
        )
        await repo.publish(
            session,
            row,
            IncidentEventKind.CREATED,
            at=at,
            correlation_id=corr,
            cause_event_id=event.event_id,
        )
    elif decision is Decision.ATTACH:
        row = _required(open_row)
        _merge_alert(row, event)
        await repo.publish(
            session,
            row,
            IncidentEventKind.ALERT_ATTACHED,
            at=at,
            correlation_id=corr,
            cause_event_id=event.event_id,
        )
    elif decision is Decision.REOPEN:
        row = await repo.transition(
            session,
            _required(resolved_row).id,
            IncidentStatus.OPEN,
            actor=ACTOR,
            reason=f"alert {event.alertname} recurred within reopen window",
            correlation_id=corr,
            at=at,
        )
        _merge_alert(row, event)
        await session.flush()
    elif decision is Decision.CLEAR:
        row = _required(open_row)
        row.alert_active = False
        row.last_seen = max(row.last_seen, event.ends_at or event.received_at)
        await repo.publish(
            session,
            row,
            IncidentEventKind.ALERT_CLEARED,
            at=at,
            correlation_id=corr,
            cause_event_id=event.event_id,
        )

    if (
        decision is Decision.IGNORE
        and resolved_row is not None
        and event.status.value == "resolved"
    ):
        # The alert cleared after the incident was already closed: record it, no transition.
        resolved_row.alert_active = False
        row = resolved_row

    if row is not None:
        await session.execute(
            update(AlertEventRow)
            .where(AlertEventRow.event_id == event.event_id)
            .values(incident_id=row.id)
        )
    logger.info(
        "alert correlated",
        extra={
            "event_id": event.event_id,
            "fingerprint": event.fingerprint,
            "decision": decision.value,
            "incident_id": str(row.id) if row else None,
        },
    )
    return decision, row


def _required(row: IncidentRow | None) -> IncidentRow:
    if row is None:  # decide() guarantees this; guard against future edits
        raise RuntimeError("correlation decision requires an incident row")
    return row


def _merge_alert(row: IncidentRow, event: AlertEvent) -> None:
    from sentinel_contracts import Severity

    row.alert_active = True
    row.alert_count += 1
    row.last_seen = max(row.last_seen, event.starts_at)
    if event.severity.rank > Severity(row.severity).rank:
        row.severity = event.severity.value


def make_handler(sessions: async_sessionmaker[AsyncSession], reopen_window: timedelta) -> Any:
    async def handle(record: ConsumerRecord[bytes, bytes], value: dict[str, Any]) -> None:
        try:
            event = AlertEvent.model_validate(value)
        except ValidationError as exc:
            raise PermanentError(f"invalid AlertEvent: {exc.error_count()} errors") from exc
        async with transaction(sessions) as session:
            await apply_event(session, event, reopen_window=reopen_window)

    return handle


def main() -> None:  # pragma: no cover - process entrypoint
    import os

    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_storage import create_engine, session_factory

    handle_tel = setup_telemetry(TelemetrySettings())
    window = timedelta(minutes=float(os.environ.get("REOPEN_WINDOW_MINUTES", "30")))

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        broker = BrokerSettings()
        await ensure_topics(broker, [t.value for t in Topic])
        engine = create_engine()
        producer = Producer(broker)
        await producer.start()
        try:
            await run_consumer(
                settings=broker,
                topics=[Topic.ALERTS.value],
                group_id="sentinel-correlator",
                handler=make_handler(session_factory(engine), window),
                stop=stop,
                dlq_producer=producer,
            )
        finally:
            await producer.stop()
            await engine.dispose()
            handle_tel.shutdown()

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
