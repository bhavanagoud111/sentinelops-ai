"""Outbox relay (ADR-004).

Loop: claim a batch of unpublished outbox rows (FOR UPDATE SKIP LOCKED), publish
each to Kafka keyed by its row key, mark the published ones, commit. A crash
between publish and commit re-publishes those rows on restart — consumers
deduplicate (at-least-once, never lost).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime
from typing import Protocol

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from sentinel_contracts import Topic
from sentinel_messaging import BrokerSettings, Producer, ensure_topics
from sentinel_storage import outbox
from sentinel_storage.db import transaction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("sentinel_relay")
meter = metrics.get_meter("sentinel_relay")
_published = meter.create_counter("sentinel_outbox_published", description="Rows published")
_failures = meter.create_counter("sentinel_outbox_publish_failures")

BATCH = 100
IDLE_SLEEP_S = 0.2


class Sender(Protocol):
    async def send(
        self, topic: str, key: str, value: dict[str, object], headers: dict[str, str] | None = ...
    ) -> None: ...


async def relay_once(sessions: async_sessionmaker[AsyncSession], sender: Sender) -> int:
    """Publish one batch. Returns the number of rows published."""
    async with transaction(sessions) as session:
        rows = await outbox.claim_batch(session, BATCH)
        published: list[int] = []
        for row in rows:
            try:
                await sender.send(row.topic, row.key, dict(row.payload), dict(row.headers))
            except Exception as exc:
                _failures.add(1, {"topic": row.topic})
                logger.warning(
                    "publish failed; will retry",
                    extra={"outbox_id": row.id, "topic": row.topic, "error": repr(exc)[:300]},
                )
                await outbox.mark_failed(session, row.id, repr(exc))
                break  # preserve per-key ordering: stop at first failure
            published.append(row.id)
            _published.add(1, {"topic": row.topic})
        await outbox.mark_published(session, published, datetime.now(UTC))
        return len(published)


async def run(
    sessions: async_sessionmaker[AsyncSession], sender: Sender, stop: asyncio.Event
) -> None:
    backoff = IDLE_SLEEP_S
    while not stop.is_set():
        try:
            n = await relay_once(sessions, sender)
            backoff = IDLE_SLEEP_S
        except Exception:
            logger.exception("relay iteration failed")
            n = 0
            backoff = min(backoff * 2, 10.0)
        if n < BATCH:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)


def main() -> None:  # pragma: no cover - process entrypoint
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_storage import create_engine, session_factory

    handle = setup_telemetry(TelemetrySettings())

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        broker = BrokerSettings()
        await ensure_topics(broker, [t.value for t in Topic])
        engine = create_engine()
        sessions = session_factory(engine)

        _backlog_value = [0]

        def _backlog_cb(_: CallbackOptions) -> list[Observation]:
            return [Observation(_backlog_value[0])]

        meter.create_observable_gauge(
            "sentinel_outbox_backlog", callbacks=[_backlog_cb], description="Unpublished rows"
        )

        async def sample_backlog() -> None:
            while not stop.is_set():
                try:
                    async with transaction(sessions) as s:
                        _backlog_value[0] = await outbox.unpublished_count(s)
                except Exception:
                    logger.exception("backlog sample failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)

        producer = Producer(broker)
        await producer.start()
        logger.info("relay started")
        try:
            await asyncio.gather(run(sessions, producer, stop), sample_backlog())
        finally:
            await producer.stop()
            await engine.dispose()
            handle.shutdown()

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
