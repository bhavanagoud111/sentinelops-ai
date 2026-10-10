"""Kafka-protocol messaging (ADR-002).

* :class:`Producer` — idempotent producer (acks=all), JSON values, string keys.
* :func:`run_consumer` — at-least-once consume loop: manual commits after the
  handler succeeds, bounded retries with backoff, then dead-letter to
  ``<topic>.dlq`` so one poison message cannot block a partition.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from opentelemetry import metrics, trace
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("sentinel_messaging")
meter = metrics.get_meter("sentinel_messaging")
_consumed = meter.create_counter("sentinel_messages_consumed", description="Messages handled")
_dead_lettered = meter.create_counter("sentinel_messages_dead_lettered")
_produced = meter.create_counter("sentinel_messages_produced")


class BrokerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    bootstrap_servers: str = Field(default="localhost:19092", validation_alias="KAFKA_BOOTSTRAP")
    client_id: str = Field(default="sentinelops", validation_alias="KAFKA_CLIENT_ID")


Handler = Callable[[ConsumerRecord[bytes, bytes], dict[str, Any]], Awaitable[None]]


def _encode(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode()


class Producer:
    def __init__(self, settings: BrokerSettings | None = None) -> None:
        cfg = settings or BrokerSettings()
        self._producer = AIOKafkaProducer(
            bootstrap_servers=cfg.bootstrap_servers,
            client_id=cfg.client_id,
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
        )

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        await self._producer.stop()

    async def send(
        self,
        topic: str,
        key: str,
        value: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        hdrs = [(k, v.encode()) for k, v in (headers or {}).items()]
        await self._producer.send_and_wait(topic, _encode(value), key=key.encode(), headers=hdrs)
        _produced.add(1, {"topic": topic})


async def ensure_topics(
    settings: BrokerSettings, topics: list[str], partitions: int = 3, retries: int = 30
) -> None:
    """Create topics (and their DLQs) if missing. Retries while the broker starts."""
    for attempt in range(retries):
        admin = AIOKafkaAdminClient(bootstrap_servers=settings.bootstrap_servers)
        try:
            await admin.start()
            existing = set(await admin.list_topics())
            wanted = [t for name in topics for t in (name, f"{name}.dlq") if t not in existing]
            if wanted:
                with contextlib.suppress(TopicAlreadyExistsError):
                    await admin.create_topics(
                        [
                            NewTopic(t, num_partitions=partitions, replication_factor=1)
                            for t in wanted
                        ]
                    )
                logger.info("topics created", extra={"topics": wanted})
            return
        except Exception as exc:  # broker not ready yet
            logger.warning(
                "broker not ready", extra={"attempt": attempt + 1, "error": repr(exc)[:200]}
            )
            await asyncio.sleep(min(2 * (attempt + 1), 10))
        finally:
            await admin.close()
    raise RuntimeError("could not reach Kafka broker to ensure topics")


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def delay(self, attempt: int) -> float:
        return float(min(self.base_delay_s * (2**attempt), self.max_delay_s))


async def run_consumer(
    *,
    settings: BrokerSettings,
    topics: list[str],
    group_id: str,
    handler: Handler,
    stop: asyncio.Event,
    dlq_producer: Producer,
    retry: RetryPolicy | None = None,
    auto_offset_reset: str = "earliest",
) -> None:
    policy = retry or RetryPolicy()
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=settings.bootstrap_servers,
        group_id=group_id,
        client_id=f"{settings.client_id}-{group_id}",
        enable_auto_commit=False,
        auto_offset_reset=auto_offset_reset,
        isolation_level="read_committed",
    )
    await consumer.start()
    logger.info("consumer started", extra={"group_id": group_id, "topics": topics})
    try:
        while not stop.is_set():
            batches = await consumer.getmany(timeout_ms=500, max_records=100)
            for tp, records in batches.items():
                for record in records:
                    await _handle_one(record, handler, policy, dlq_producer, group_id)
                    # Commit after each record: on crash we redeliver at most one message,
                    # which the handler's idempotency ledger absorbs.
                    await consumer.commit({tp: record.offset + 1})
    finally:
        await consumer.stop()
        logger.info("consumer stopped", extra={"group_id": group_id})


async def _handle_one(
    record: ConsumerRecord[bytes, bytes],
    handler: Handler,
    policy: RetryPolicy,
    dlq: Producer,
    group_id: str,
) -> None:
    try:
        value = json.loads(record.value or b"{}")
        if not isinstance(value, dict):
            raise ValueError("message value must be a JSON object")
    except ValueError as exc:
        await _dead_letter(record, dlq, group_id, f"undecodable: {exc}")
        return

    for attempt in range(policy.attempts):
        with tracer.start_as_current_span(
            f"consume {record.topic}",
            attributes={
                "messaging.system": "kafka",
                "messaging.destination.name": record.topic,
                "messaging.consumer.group.name": group_id,
                "messaging.kafka.offset": record.offset,
            },
        ):
            try:
                await handler(record, value)
                _consumed.add(1, {"topic": record.topic, "group": group_id, "outcome": "ok"})
                return
            except PermanentError as exc:
                await _dead_letter(record, dlq, group_id, f"permanent: {exc}")
                return
            except Exception as exc:
                logger.warning(
                    "handler failed; will retry",
                    extra={
                        "topic": record.topic,
                        "offset": record.offset,
                        "attempt": attempt + 1,
                        "error": repr(exc)[:500],
                    },
                )
                if attempt + 1 < policy.attempts:
                    await asyncio.sleep(policy.delay(attempt))
    await _dead_letter(record, dlq, group_id, "retries exhausted")


async def _dead_letter(
    record: ConsumerRecord[bytes, bytes], dlq: Producer, group_id: str, reason: str
) -> None:
    _dead_lettered.add(1, {"topic": record.topic, "group": group_id})
    _consumed.add(1, {"topic": record.topic, "group": group_id, "outcome": "dead_lettered"})
    logger.error(
        "message dead-lettered",
        extra={"topic": record.topic, "offset": record.offset, "reason": reason},
    )
    await dlq.send(
        f"{record.topic}.dlq",
        (record.key or b"").decode(errors="replace"),
        {
            "reason": reason,
            "group_id": group_id,
            "source_topic": record.topic,
            "source_partition": record.partition,
            "source_offset": record.offset,
            "value": (record.value or b"").decode(errors="replace")[:100_000],
        },
    )


class PermanentError(Exception):
    """Raise from a handler for errors retries cannot fix (bad schema, invariant)."""


__all__ = [
    "BrokerSettings",
    "PermanentError",
    "Producer",
    "RetryPolicy",
    "ensure_topics",
    "run_consumer",
]
