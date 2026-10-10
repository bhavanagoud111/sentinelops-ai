"""Investigator service: consumes ``sentinel.incidents.v1`` and runs investigations.

Lifecycle per incident (spec section 8):
    OPEN → TRIAGING → INVESTIGATING → AWAITING_APPROVAL   (remediation proposed)
                                    → NEEDS_HUMAN         (escalated / abstained / failed)

tx1 claims the message, moves the incident to INVESTIGATING and inserts a
``running`` investigation (unique per incident). The graph runs outside any
transaction. tx2 persists evidence, hypotheses and the outcome, transitions the
incident, and publishes an InvestigationEvent via the outbox. A sweeper fails
investigations orphaned by a crash between tx1 and tx2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from aiokafka import ConsumerRecord
from opentelemetry import metrics, trace
from pydantic import ValidationError
from sentinel_contracts import (
    IncidentEvent,
    IncidentEventKind,
    IncidentStatus,
    InvestigationEvent,
    Topic,
)
from sentinel_messaging import BrokerSettings, PermanentError, Producer, ensure_topics, run_consumer
from sentinel_policy import describe_catalog
from sentinel_storage import audit, dedup, outbox
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import EvidenceRow, HypothesisRow, InvestigationRow, Service
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_investigator.graph import Deps, Limits, run_investigation
from sentinel_investigator.models import InvestigationOutcome, InvestigationResult
from sentinel_investigator.providers.base import ModelProvider
from sentinel_investigator.tools import Toolbox

logger = logging.getLogger("sentinel_investigator")
tracer = trace.get_tracer("sentinel_investigator")
meter = metrics.get_meter("sentinel_investigator")
_runs = meter.create_counter("sentinel_investigations", description="Investigations by outcome")
_duration = meter.create_histogram(
    "sentinel_investigation_duration", unit="s", description="Wall-clock investigation time"
)
_tokens = meter.create_counter("sentinel_llm_tokens", description="LLM tokens by type")

CONSUMER = "investigator"
ACTOR = "system:investigator"
AUTO_KINDS = {IncidentEventKind.CREATED}
REINVESTIGABLE = {
    IncidentStatus.OPEN,
    IncidentStatus.NEEDS_HUMAN,
    IncidentStatus.REJECTED,
    IncidentStatus.FAILED,
}


class Investigator:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: ModelProvider,
        toolbox: Toolbox,
        limits: Limits | None = None,
        auto_investigate: bool = True,
    ) -> None:
        self.sessions = sessions
        self.provider = provider
        self.toolbox = toolbox
        self.limits = limits or Limits()
        self.auto = auto_investigate

    # --------------------------------------------------------------- entry
    async def handle(self, event: IncidentEvent) -> InvestigationOutcome | None:
        wanted = (
            (event.kind in AUTO_KINDS and self.auto)
            or (event.kind is IncidentEventKind.INVESTIGATION_REQUESTED)
            or (
                event.kind is IncidentEventKind.STATUS_CHANGED
                and self.auto
                and event.previous_status is IncidentStatus.RESOLVED
                and event.incident.status is IncidentStatus.OPEN
            )
        )
        async with transaction(self.sessions) as s:
            if not await dedup.claim(s, CONSUMER, event.event_id):
                return None
            if not wanted:
                return None
            started = await self._begin(s, event)
        if started is None:
            return None
        investigation_id, incident = started
        t0 = asyncio.get_running_loop().time()
        with tracer.start_as_current_span(
            "investigate",
            attributes={"incident.id": str(incident.id), "provider": self.provider.name},
        ):
            deps = await self._deps()
            result = await run_investigation(incident, deps)
        _duration.record(asyncio.get_running_loop().time() - t0, {"outcome": result.outcome.value})
        _runs.add(1, {"outcome": result.outcome.value, "provider": self.provider.name})
        for step in result.steps:
            for k, v in step.usage.items():
                _tokens.add(v, {"type": k, "provider": self.provider.name})
        await self._finish(investigation_id, incident.id, result, event.correlation_id)
        return result.outcome

    async def _begin(self, s: AsyncSession, event: IncidentEvent) -> tuple[uuid.UUID, Any] | None:
        now = datetime.now(UTC)
        row = await repo.get(s, event.incident.id, lock=True)
        status = IncidentStatus(row.status)
        if status not in REINVESTIGABLE:
            logger.info(
                "incident not investigable in current state",
                extra={"incident_id": str(row.id), "status": status.value},
            )
            return None
        running = await s.scalar(
            select(InvestigationRow.id).where(
                InvestigationRow.incident_id == row.id, InvestigationRow.status == "running"
            )
        )
        if running:
            return None
        corr = event.correlation_id
        if status is IncidentStatus.OPEN:
            await repo.transition(
                s,
                row.id,
                IncidentStatus.TRIAGING,
                actor=ACTOR,
                reason="investigation started",
                correlation_id=corr,
                at=now,
            )
            status = IncidentStatus.TRIAGING
        await repo.transition(
            s,
            row.id,
            IncidentStatus.INVESTIGATING,
            actor=ACTOR,
            reason=f"collecting evidence ({self.provider.name})",
            correlation_id=corr,
            at=now,
        )
        inv = InvestigationRow(
            incident_id=row.id,
            status="running",
            provider=self.provider.name,
            trigger=(
                "manual" if event.kind is IncidentEventKind.INVESTIGATION_REQUESTED else "auto"
            )
            + f":{event.event_id}"[:120],
            started_at=now,
            summary="",
            triage={},
            steps=[],
            usage={},
        )
        s.add(inv)
        await s.flush()
        return inv.id, repo.to_contract(row)

    async def _deps(self) -> Deps:
        async with self.sessions() as s:
            services = (await s.scalars(select(Service))).all()
        return Deps(
            provider=self.provider,
            toolbox=self.toolbox,
            catalog=describe_catalog(),
            service_graph={sv.name: [str(d) for d in sv.dependencies] for sv in services},
            criticality={sv.name: sv.criticality for sv in services},
            limits=self.limits,
        )

    async def _finish(
        self,
        investigation_id: uuid.UUID,
        incident_id: uuid.UUID,
        result: InvestigationResult,
        correlation_id: str,
    ) -> None:
        now = datetime.now(UTC)
        findings = {f.hypothesis_id: f for f in result.findings}
        usage: dict[str, int] = {}
        for st in result.steps:
            for k, v in st.usage.items():
                usage[k] = usage.get(k, 0) + v
        async with transaction(self.sessions) as s:
            for e in result.evidence:
                await s.execute(
                    insert(EvidenceRow)
                    .values(
                        id=e.evidence_id,
                        incident_id=incident_id,
                        investigation_id=investigation_id,
                        kind=e.kind.value,
                        service=e.service,
                        source=e.source,
                        tool=e.tool,
                        query_ref=e.query,
                        range_start=e.start,
                        range_end=e.end,
                        retrieved_at=e.retrieved_at,
                        content_hash=e.content_hash,
                        summary=e.summary,
                        anomalous=e.anomalous,
                        signals=_jsonable(e.signals),
                        error=e.error,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            for rank, h in enumerate(result.hypotheses, start=1):
                f = findings.get(h.hypothesis_id)
                s.add(
                    HypothesisRow(
                        incident_id=incident_id,
                        investigation_id=investigation_id,
                        key=h.hypothesis_id,
                        rank=rank,
                        cause_category=h.cause_category.value,
                        suspected_service=h.suspected_service,
                        description=h.explanation,
                        confidence=h.confidence,
                        adjusted_confidence=f.adjusted_confidence if f else 0.0,
                        verdict=f.verdict.value if f else "rejected",
                        critic_reasons=f.reasons if f else ["not reviewed"],
                        evidence_refs=h.evidence_ids,
                        counterevidence_refs=h.counterevidence_ids,
                        recommended_runbook_id=h.recommended_runbook_id,
                        runbook_params=_jsonable(h.runbook_params),
                        uncertainty=h.uncertainty,
                        next_checks=h.next_checks,
                    )
                )
            await s.execute(
                update(InvestigationRow)
                .where(InvestigationRow.id == investigation_id)
                .values(
                    status=result.outcome.value,
                    finished_at=now,
                    summary=result.summary[:8000],
                    triage=result.triage.model_dump(mode="json") if result.triage else {},
                    steps=[st.model_dump(mode="json") for st in result.steps],
                    proposal=result.proposal.model_dump(mode="json") if result.proposal else None,
                    usage=usage,
                    error=result.error,
                )
            )
            row = await repo.get(s, incident_id, lock=True)
            row.summary = result.summary[:4000]
            if IncidentStatus(row.status) is IncidentStatus.INVESTIGATING:
                target = (
                    IncidentStatus.AWAITING_APPROVAL
                    if result.outcome is InvestigationOutcome.PROPOSED
                    else IncidentStatus.NEEDS_HUMAN
                )
                await repo.transition(
                    s,
                    incident_id,
                    target,
                    actor=ACTOR,
                    reason=f"investigation {result.outcome.value}",
                    correlation_id=correlation_id,
                    at=now,
                )
            await audit.record(
                s,
                actor=ACTOR,
                action="investigation.completed",
                resource_type="investigation",
                resource_id=str(investigation_id),
                at=now,
                details={
                    "incident_id": str(incident_id),
                    "outcome": result.outcome.value,
                    "provider": result.provider,
                    "evidence": len(result.evidence),
                    "proposal": result.proposal.runbook_id if result.proposal else None,
                },
            )
            top = next(
                (
                    h
                    for h in result.hypotheses
                    if findings.get(h.hypothesis_id)
                    and findings[h.hypothesis_id].verdict.value != "rejected"
                ),
                None,
            )
            await outbox.enqueue(
                s,
                Topic.INVESTIGATIONS,
                str(incident_id),
                InvestigationEvent(
                    event_id=f"investigation:{investigation_id}",
                    incident_id=incident_id,
                    investigation_id=investigation_id,
                    outcome=result.outcome.value,
                    provider=result.provider,
                    summary=result.summary[:4000],
                    proposal=result.proposal.model_dump(mode="json") if result.proposal else None,
                    top_hypothesis=(
                        {
                            "category": top.cause_category.value,
                            "service": top.suspected_service,
                            "confidence": findings[top.hypothesis_id].adjusted_confidence,
                        }
                        if top
                        else None
                    ),
                    correlation_id=correlation_id,
                    occurred_at=now,
                ),
            )
        logger.info(
            "investigation finished",
            extra={
                "incident_id": str(incident_id),
                "outcome": result.outcome.value,
                "evidence": len(result.evidence),
            },
        )

    async def sweep_orphans(self, older_than: timedelta) -> int:
        """Fail investigations left ``running`` by a crash; hand the incident to a human."""
        cutoff = datetime.now(UTC) - older_than
        n = 0
        async with transaction(self.sessions) as s:
            rows = (
                await s.scalars(
                    select(InvestigationRow)
                    .where(
                        InvestigationRow.status == "running", InvestigationRow.started_at < cutoff
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for inv in rows:
                inv.status = "failed"
                inv.finished_at = datetime.now(UTC)
                inv.error = "orphaned: investigator stopped before completion"
                inc = await repo.get(s, inv.incident_id, lock=True)
                if IncidentStatus(inc.status) is IncidentStatus.INVESTIGATING:
                    await repo.transition(
                        s,
                        inc.id,
                        IncidentStatus.NEEDS_HUMAN,
                        actor=ACTOR,
                        reason="investigation orphaned",
                        correlation_id=str(inv.id),
                        at=datetime.now(UTC),
                    )
                n += 1
        return n


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    import json

    return dict(json.loads(json.dumps(d, default=str)))


def make_provider() -> ModelProvider:
    name = os.environ.get("SENTINEL_LLM_PROVIDER", "rules").lower()
    if name == "claude":
        from sentinel_investigator.providers.claude import ClaudeProvider

        return ClaudeProvider()
    from sentinel_investigator.providers.rules import RulesProvider

    return RulesProvider()


def main() -> None:  # pragma: no cover - process entrypoint
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_storage import create_engine, session_factory
    from sentinel_telemetry import (
        LokiClient,
        PrometheusClient,
        TelemetrySourcesSettings,
        TempoClient,
    )

    from sentinel_investigator.k8s import kubernetes_reader_from_env
    from sentinel_investigator.tools import LiveToolbox

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
        provider = make_provider()
        limits = Limits(
            deadline_s=float(os.environ.get("INVESTIGATION_DEADLINE_S", "120")),
            model_critic=os.environ.get("SENTINEL_LLM_CRITIC", "true").lower() == "true",
        )
        inv = Investigator(
            session_factory(engine),
            provider,
            toolbox,
            limits,
            auto_investigate=os.environ.get("AUTO_INVESTIGATE", "true").lower() == "true",
        )
        logger.info("investigator started", extra={"provider": provider.name})

        async def sweeper() -> None:
            while not stop.is_set():
                try:
                    n = await inv.sweep_orphans(timedelta(seconds=limits.deadline_s * 3))
                    if n:
                        logger.warning("orphaned investigations failed", extra={"count": n})
                except Exception:
                    logger.exception("sweeper failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=60)

        async def handle(record: ConsumerRecord[bytes, bytes], value: dict[str, Any]) -> None:
            try:
                event = IncidentEvent.model_validate(value)
            except ValidationError as exc:
                raise PermanentError(f"invalid IncidentEvent: {exc.error_count()} errors") from exc
            try:
                await inv.handle(event)
            except IntegrityError:
                logger.info(
                    "investigation already running; skipping",
                    extra={"incident_id": str(event.incident.id)},
                )

        producer = Producer(broker)
        await producer.start()
        try:
            await asyncio.gather(
                run_consumer(
                    settings=broker,
                    topics=[Topic.INCIDENTS.value],
                    group_id="sentinel-investigator",
                    handler=handle,
                    stop=stop,
                    dlq_producer=producer,
                ),
                sweeper(),
            )
        finally:
            await producer.stop()
            await engine.dispose()
            handle_tel.shutdown()

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
