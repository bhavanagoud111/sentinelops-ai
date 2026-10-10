"""Verifier: confirms recovery after a successful action (spec section 8).

Consumes ``sentinel.actions.v1`` (kind=succeeded). Re-collects the evidence that
defines the incident's symptom (e.g. the 5xx ratio for a 5xx alert) plus the
target's pod health, and requires ``REQUIRED_PASSES`` consecutive healthy checks.
VERIFYING → RESOLVED on success, → FAILED (human follow-up) on timeout. A cleared
alert alone is never treated as proof of recovery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiokafka import ConsumerRecord
from opentelemetry import metrics
from pydantic import ValidationError
from sentinel_contracts import ActionEvent, ActionEventKind, IncidentStatus, Topic
from sentinel_investigator.models import Evidence, EvidenceKind
from sentinel_investigator.tools import Toolbox, ToolContext
from sentinel_messaging import BrokerSettings, PermanentError, Producer, ensure_topics, run_consumer
from sentinel_storage import audit, dedup, outbox
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import ExecutionRow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("sentinel_verifier")
meter = metrics.get_meter("sentinel_verifier")
_verifications = meter.create_counter("sentinel_verifications", description="By outcome")
_mttr = meter.create_histogram(
    "sentinel_time_to_verified_recovery",
    unit="s",
    description="Incident first_seen to verified recovery",
)

CONSUMER = "verifier"
ACTOR = "system:verifier"
K = EvidenceKind


@dataclass(frozen=True)
class VerifyConfig:
    initial_wait_s: float = 30.0
    interval_s: float = 20.0
    required_passes: int = 2
    timeout_s: float = 240.0  # stays under the consumer's max poll interval
    window: timedelta = timedelta(minutes=3)


def symptom_kinds(alertname: str) -> list[EvidenceKind]:
    """Which evidence defines 'recovered' for this alert."""
    name = alertname.lower()
    if "crashloop" in name:
        return [K.POD_STATUS]
    if "memory" in name:
        return [K.PROCESS_MEMORY]
    if "latency" in name:
        return [K.LATENCY_P95]
    if "5xx" in name or "error" in name:
        return [K.ERROR_RATIO]
    return [K.ERROR_RATIO, K.LATENCY_P95]


def healthy(e: Evidence) -> tuple[bool, str]:
    if e.error:
        return False, f"{e.kind.value}@{e.service}: unavailable ({e.error[:80]})"
    if e.kind is K.POD_STATUS:
        s = e.signals
        ok = (
            not s.get("waiting_reasons")
            and not s.get("crash_looping")
            and s.get("ready", 0) >= s.get("desired", 1) > 0
        )
        return (
            ok,
            f"pods {s.get('ready')}/{s.get('desired')} ready, waiting={s.get('waiting_reasons')}",
        )
    active = bool(e.signals.get("active"))
    latest, threshold = e.signals.get("latest"), e.signals.get("threshold")
    state = "still above" if active else "below"
    return (
        not active
    ), f"{e.kind.value}@{e.service}: latest {latest} {state} threshold {threshold}"


class Verifier:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        toolbox: Toolbox,
        cfg: VerifyConfig | None = None,
    ) -> None:
        self.sessions = sessions
        self.toolbox = toolbox
        self.cfg = cfg or VerifyConfig()

    async def handle(self, event: ActionEvent) -> str | None:
        if event.kind is not ActionEventKind.SUCCEEDED:
            return None
        async with transaction(self.sessions) as s:
            if not await dedup.claim(s, CONSUMER, event.event_id):
                return None
            ex = await s.scalar(select(ExecutionRow).where(ExecutionRow.id == event.execution_id))
            inc = await repo.get(s, event.incident_id)
            if ex is None or inc.status != IncidentStatus.VERIFYING.value:
                return None
            alertname = inc.fingerprint.rsplit(":", 1)[-1]
            service, target = inc.service_name, ex.target.split("/", 1)[1]
            first_seen = inc.first_seen
        return await self.verify(
            event.execution_id, event.incident_id, alertname, service, target, first_seen
        )

    async def verify(
        self,
        execution_id: uuid.UUID,
        incident_id: uuid.UUID,
        alertname: str,
        service: str,
        target: str,
        first_seen: datetime,
    ) -> str:
        cfg = self.cfg
        checks: list[tuple[EvidenceKind, str]] = [(k, service) for k in symptom_kinds(alertname)]
        if (K.POD_STATUS, target) not in checks:
            checks.append((K.POD_STATUS, target))
        await asyncio.sleep(cfg.initial_wait_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.timeout_s
        passes = 0
        rounds: list[dict[str, Any]] = []
        while True:
            end = datetime.now(UTC)
            ctx = ToolContext(
                incident_id=incident_id,
                scope=frozenset({service, target}),
                start=end - cfg.window,
                end=end,
            )
            results = [await self.toolbox.collect(k, svc, ctx) for k, svc in checks]
            verdicts = [healthy(e) for e in results]
            ok = all(v for v, _ in verdicts)
            passes = passes + 1 if ok else 0
            rounds.append(
                {
                    "at": end.isoformat(),
                    "healthy": ok,
                    "checks": [d for _, d in verdicts],
                    "evidence_ids": [e.evidence_id for e in results],
                }
            )
            if passes >= cfg.required_passes or loop.time() >= deadline:
                break
            await asyncio.sleep(cfg.interval_s)

        recovered = passes >= cfg.required_passes
        now = datetime.now(UTC)
        async with transaction(self.sessions) as s:
            ex = await s.scalar(
                select(ExecutionRow).where(ExecutionRow.id == execution_id).with_for_update()
            )
            if ex is not None:
                ex.verification = {
                    "recovered": recovered,
                    "rounds": rounds[-6:],
                    "required_passes": cfg.required_passes,
                }
            target_status = IncidentStatus.RESOLVED if recovered else IncidentStatus.FAILED
            inc = await repo.get(s, incident_id, lock=True)
            if inc.status == IncidentStatus.VERIFYING.value:
                await repo.transition(
                    s,
                    incident_id,
                    target_status,
                    actor=ACTOR,
                    reason=("recovery verified: " if recovered else "recovery not verified: ")
                    + "; ".join(rounds[-1]["checks"])[:800],
                    correlation_id=str(execution_id),
                    at=now,
                )
            kind = ActionEventKind.VERIFIED if recovered else ActionEventKind.VERIFICATION_FAILED
            if ex is not None:
                await outbox.enqueue(
                    s,
                    Topic.ACTIONS,
                    str(incident_id),
                    ActionEvent(
                        event_id=f"action:{execution_id}:{kind.value}",
                        kind=kind,
                        execution_id=execution_id,
                        incident_id=incident_id,
                        approval_id=ex.approval_id,
                        runbook_id=ex.runbook_id,
                        actor=ACTOR,
                        details={"rounds": len(rounds)},
                        occurred_at=now,
                    ),
                )
            await audit.record(
                s,
                actor=ACTOR,
                action=f"verification.{'passed' if recovered else 'failed'}",
                resource_type="execution",
                resource_id=str(execution_id),
                at=now,
                details={"incident_id": str(incident_id), "last": rounds[-1]},
            )
        outcome = "recovered" if recovered else "not_recovered"
        _verifications.add(1, {"outcome": outcome})
        if recovered:
            _mttr.record((now - first_seen).total_seconds())
        logger.info(
            "verification finished",
            extra={"incident_id": str(incident_id), "outcome": outcome, "rounds": len(rounds)},
        )
        return outcome


def main() -> None:  # pragma: no cover - process entrypoint
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_investigator.k8s import kubernetes_reader_from_env
    from sentinel_investigator.tools import LiveToolbox
    from sentinel_storage import create_engine, session_factory
    from sentinel_telemetry import (
        LokiClient,
        PrometheusClient,
        TelemetrySourcesSettings,
        TempoClient,
    )

    handle_tel = setup_telemetry(TelemetrySettings())

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        broker = BrokerSettings()
        await ensure_topics(broker, [t.value for t in Topic])
        src = TelemetrySourcesSettings()
        toolbox = LiveToolbox(
            PrometheusClient(src.prometheus_url, src.timeout_s),
            LokiClient(src.loki_url, src.timeout_s),
            TempoClient(src.tempo_url, src.timeout_s),
            kubernetes_reader_from_env(),
        )
        engine = create_engine()
        verifier = Verifier(
            session_factory(engine),
            toolbox,
            VerifyConfig(
                initial_wait_s=float(os.environ.get("VERIFY_INITIAL_WAIT_S", "30")),
                timeout_s=float(os.environ.get("VERIFY_TIMEOUT_S", "240")),
            ),
        )
        logger.info("verifier started")

        async def handle(record: ConsumerRecord[bytes, bytes], value: dict[str, Any]) -> None:
            try:
                event = ActionEvent.model_validate(value)
            except ValidationError as exc:
                raise PermanentError(f"invalid ActionEvent: {exc.error_count()} errors") from exc
            await verifier.handle(event)

        producer = Producer(broker)
        await producer.start()
        try:
            await run_consumer(
                settings=broker,
                topics=[Topic.ACTIONS.value],
                group_id="sentinel-verifier",
                handler=handle,
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
