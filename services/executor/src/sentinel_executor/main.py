"""Executor: consumes ``sentinel.actions.v1`` (kind=requested) and performs one action.

Per execution:
 1. claim the message (idempotent) and lock the execution row (must be ``pending``)
 2. take a live deployment snapshot and run the policy engine (time-of-use check)
 3. denied  → execution ``blocked``, incident REMEDIATING → NEEDS_HUMAN, audit
    allowed → execution ``running``, dry-run + patch, wait for rollout
 4. success → incident REMEDIATING → VERIFYING; failure → REMEDIATING → FAILED
Every step is audited (hash chain) and published as an ActionEvent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from aiokafka import ConsumerRecord
from opentelemetry import metrics, trace
from pydantic import ValidationError
from sentinel_contracts import ActionEvent, ActionEventKind, IncidentStatus, Topic
from sentinel_messaging import BrokerSettings, PermanentError, Producer, ensure_topics, run_consumer
from sentinel_policy import RunbookParams
from sentinel_policy.engine import (
    ApprovalSnapshot,
    DeploymentSnapshot,
    PolicyContext,
    RemediationMode,
    evaluate,
)
from sentinel_storage import audit, dedup, outbox
from sentinel_storage import incidents as repo
from sentinel_storage.db import transaction
from sentinel_storage.models import ApprovalRow, ExecutionRow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_executor.k8s_actions import ActionResult

logger = logging.getLogger("sentinel_executor")
tracer = trace.get_tracer("sentinel_executor")
meter = metrics.get_meter("sentinel_executor")
_actions = meter.create_counter("sentinel_actions", description="Executions by outcome")

CONSUMER = "executor"
ACTOR = "system:executor"


class Cluster(Protocol):
    async def snapshot(self, namespace: str, name: str) -> DeploymentSnapshot: ...

    async def apply(
        self, params: RunbookParams, rollout_timeout_s: float = ...
    ) -> ActionResult: ...


class Executor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cluster: Cluster,
        mode: RemediationMode,
        rollout_timeout_s: float = 180.0,
    ) -> None:
        self.sessions = sessions
        self.cluster = cluster
        self.mode = mode
        self.rollout_timeout_s = rollout_timeout_s

    async def handle(self, event: ActionEvent) -> str | None:
        if event.kind is not ActionEventKind.REQUESTED:
            return None
        async with transaction(self.sessions) as s:
            if not await dedup.claim(s, CONSUMER, event.event_id):
                return None
        with tracer.start_as_current_span(
            "execute",
            attributes={"execution.id": str(event.execution_id), "runbook": event.runbook_id},
        ):
            return await self._execute(event.execution_id)

    async def _execute(self, execution_id: uuid.UUID) -> str:
        now = datetime.now(UTC)
        async with transaction(self.sessions) as s:
            ex = await s.scalar(
                select(ExecutionRow).where(ExecutionRow.id == execution_id).with_for_update()
            )
            if ex is None or ex.status != "pending":
                return "skipped"
            approval = await s.scalar(select(ApprovalRow).where(ApprovalRow.id == ex.approval_id))
            incident = await repo.get(s, ex.incident_id, lock=True)
            ns, name = ex.target.split("/", 1)
            active = await s.scalar(
                select(func.count())
                .select_from(ExecutionRow)
                .where(
                    ExecutionRow.target == ex.target,
                    ExecutionRow.status == "running",
                    ExecutionRow.id != ex.id,
                )
            )
            last_success = await s.scalar(
                select(func.max(ExecutionRow.finished_at)).where(
                    ExecutionRow.target == ex.target,
                    ExecutionRow.runbook_id == ex.runbook_id,
                    ExecutionRow.status == "succeeded",
                )
            )
            snap = await self.cluster.snapshot(ns, name)
            # The approval was consumed by the execute call; the policy evaluates the
            # decision it carried (approved) plus everything else, against live state.
            appr = (
                ApprovalSnapshot(
                    id=approval.id,
                    incident_id=approval.incident_id,
                    runbook_id=approval.runbook_id,
                    params=approval.params,
                    status="approved"
                    if approval.status == "consumed" and approval.approved_by
                    else approval.status,
                    requested_by=approval.requested_by,
                    approved_by=approval.approved_by,
                    expires_at=approval.expires_at,
                    request_hash=approval.request_hash,
                    target_revision=approval.target_revision,
                )
                if approval
                else None
            )
            ctx = PolicyContext(
                now=now,
                mode=self.mode,
                incident_id=incident.id,
                incident_status=incident.status,
                approval=appr,
                deployment=snap,
                active_executions_for_target=int(active or 0),
                last_success_at=last_success,
                allowed_incident_statuses=frozenset({IncidentStatus.REMEDIATING.value}),
            )
            decision = evaluate(ex.runbook_id, ex.params, ctx)
            ex.policy = decision.to_dict()
            if not decision.allowed:
                reasons = "; ".join(f"{c.name}: {c.detail}" for c in decision.failed)
                ex.status = "blocked"
                ex.error = f"policy denied: {reasons}"[:4000]
                ex.finished_at = now
                await repo.transition(
                    s,
                    incident.id,
                    IncidentStatus.NEEDS_HUMAN,
                    actor=ACTOR,
                    reason=f"action blocked by policy ({len(decision.failed)} check(s) failed)",
                    correlation_id=str(ex.id),
                    at=now,
                )
                await self._event(s, ex, ActionEventKind.BLOCKED, now, {"failed": reasons[:2000]})
                await audit.record(
                    s,
                    actor=ACTOR,
                    action="execution.blocked",
                    resource_type="execution",
                    resource_id=str(ex.id),
                    at=now,
                    details=decision.to_dict(),
                )
                _actions.add(1, {"outcome": "blocked", "runbook": ex.runbook_id})
                logger.warning(
                    "execution blocked by policy",
                    extra={"execution_id": str(ex.id), "failed": reasons[:500]},
                )
                return "blocked"
            params = decision.params
            ex.status = "running"
            ex.started_at = now
            await self._event(s, ex, ActionEventKind.STARTED, now, {})
            await audit.record(
                s,
                actor=ACTOR,
                action="execution.started",
                resource_type="execution",
                resource_id=str(ex.id),
                at=now,
                details={
                    "runbook_id": ex.runbook_id,
                    "params": ex.params,
                    "policy": "all checks passed",
                },
            )
        assert params is not None  # noqa: S101 - narrowed by decision.allowed

        try:
            result = await self.cluster.apply(params, self.rollout_timeout_s)
            error: str | None = None
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"[:2000]

        done = datetime.now(UTC)
        async with transaction(self.sessions) as s:
            ex = await s.scalar(
                select(ExecutionRow).where(ExecutionRow.id == execution_id).with_for_update()
            )
            assert ex is not None  # noqa: S101 - row exists, we just updated it
            ex.finished_at = done
            if result is not None:
                ex.status = "succeeded"
                ex.command_template = result.command_template
                ex.before_state = result.before
                ex.after_state = {**result.after, "rollout_seconds": result.rollout_seconds}
                await repo.transition(
                    s,
                    ex.incident_id,
                    IncidentStatus.VERIFYING,
                    actor=ACTOR,
                    reason=f"{ex.runbook_id} applied in {result.rollout_seconds}s; verifying",
                    correlation_id=str(ex.id),
                    at=done,
                )
                await self._event(
                    s, ex, ActionEventKind.SUCCEEDED, done, {"command": result.command_template}
                )
                outcome = "succeeded"
            else:
                ex.status = "failed"
                ex.error = error
                await repo.transition(
                    s,
                    ex.incident_id,
                    IncidentStatus.FAILED,
                    actor=ACTOR,
                    reason=f"{ex.runbook_id} failed: {error}"[:900],
                    correlation_id=str(ex.id),
                    at=done,
                )
                await self._event(s, ex, ActionEventKind.FAILED, done, {"error": error})
                outcome = "failed"
            await audit.record(
                s,
                actor=ACTOR,
                action=f"execution.{outcome}",
                resource_type="execution",
                resource_id=str(ex.id),
                at=done,
                details={
                    "command": ex.command_template,
                    "before": ex.before_state,
                    "after": ex.after_state,
                    "error": ex.error,
                },
            )
        _actions.add(1, {"outcome": outcome, "runbook": ex.runbook_id})
        logger.info(
            "execution finished",
            extra={"execution_id": str(execution_id), "outcome": outcome, "error": error},
        )
        return outcome

    async def _event(
        self,
        s: AsyncSession,
        ex: ExecutionRow,
        kind: ActionEventKind,
        at: datetime,
        details: dict[str, Any],
    ) -> None:
        await outbox.enqueue(
            s,
            Topic.ACTIONS,
            str(ex.incident_id),
            ActionEvent(
                event_id=f"action:{ex.id}:{kind.value}",
                kind=kind,
                execution_id=ex.id,
                incident_id=ex.incident_id,
                approval_id=ex.approval_id,
                runbook_id=ex.runbook_id,
                actor=ACTOR,
                details=details,
                occurred_at=at,
            ),
        )


def main() -> None:  # pragma: no cover - process entrypoint
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_storage import create_engine, session_factory

    from sentinel_executor.k8s_actions import DeploymentActions

    handle_tel = setup_telemetry(TelemetrySettings())
    mode = RemediationMode(os.environ.get("REMEDIATION_MODE", "recommend_only"))

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        broker = BrokerSettings()
        await ensure_topics(broker, [t.value for t in Topic])
        engine = create_engine()
        cluster = DeploymentActions.from_kubeconfig(os.environ["K8S_EXECUTOR_KUBECONFIG"])
        executor = Executor(session_factory(engine), cluster, mode)
        logger.info("executor started", extra={"mode": mode.value})

        async def handle(record: ConsumerRecord[bytes, bytes], value: dict[str, Any]) -> None:
            try:
                event = ActionEvent.model_validate(value)
            except ValidationError as exc:
                raise PermanentError(f"invalid ActionEvent: {exc.error_count()} errors") from exc
            await executor.handle(event)

        producer = Producer(broker)
        await producer.start()
        try:
            await run_consumer(
                settings=broker,
                topics=[Topic.ACTIONS.value],
                group_id="sentinel-executor",
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
