"""Server-Sent Events fan-out (ADR-009).

One Kafka consumer per API process (unique group id, ``latest`` offsets) reads the
lifecycle topics and broadcasts compact notifications to connected browsers. SSE
clients get "something changed" signals and refetch through the REST API, so the
stream never carries more data than the REST API would show them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sentinel_contracts import Topic

logger = logging.getLogger(__name__)

STREAM_TOPICS = [Topic.INCIDENTS, Topic.INVESTIGATIONS, Topic.APPROVALS, Topic.ACTIONS]
HEARTBEAT_S = 15.0
MAX_SUBSCRIBERS = 200


def summarize(topic: str, value: dict[str, Any]) -> dict[str, Any]:
    """Reduce an event to ids + kind/status (no payload details)."""
    inc = value.get("incident") or {}
    return {
        "topic": topic,
        "kind": value.get("kind") or value.get("outcome") or value.get("status"),
        "incident_id": str(value.get("incident_id") or inc.get("id") or ""),
        "status": inc.get("status"),
        "service": inc.get("service"),
        "occurred_at": value.get("occurred_at"),
    }


class Broadcaster:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, message: dict[str, Any]) -> None:
        for q in list(self._subs):
            if q.full():  # slow client: drop oldest rather than block everyone
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(message)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        if len(self._subs) >= MAX_SUBSCRIBERS:
            raise RuntimeError("too many stream subscribers")
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)


async def stream(broadcaster: Broadcaster, disconnected: Any) -> AsyncIterator[bytes]:
    async with broadcaster.subscribe() as q:
        yield b"retry: 3000\nevent: ready\ndata: {}\n\n"
        while True:
            if await disconnected():
                return
            try:
                msg = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                yield b": heartbeat\n\n"
                continue
            yield f"event: {msg['topic']}\ndata: {json.dumps(msg)}\n\n".encode()


async def consume(broadcaster: Broadcaster, bootstrap: str, stop: asyncio.Event) -> None:
    """Background task: Kafka → broadcaster. Reconnects with backoff; never crashes the API."""
    from aiokafka import AIOKafkaConsumer

    delay = 1.0
    while not stop.is_set():
        consumer = AIOKafkaConsumer(
            *[t.value for t in STREAM_TOPICS],
            bootstrap_servers=bootstrap,
            group_id=f"sentinel-api-sse-{uuid.uuid4().hex[:8]}",
            auto_offset_reset="latest",
            enable_auto_commit=True,
        )
        try:
            await consumer.start()
            delay = 1.0
            while not stop.is_set():
                batches = await consumer.getmany(timeout_ms=1000)
                for records in batches.values():
                    for r in records:
                        try:
                            value = json.loads(r.value or b"{}")
                        except ValueError:
                            continue
                        broadcaster.publish(summarize(r.topic, value))
        except Exception as exc:
            logger.warning("sse consumer error; reconnecting", extra={"error": repr(exc)[:200]})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        finally:
            with contextlib.suppress(Exception):
                await consumer.stop()
