"""Steady synthetic traffic generator.

Sends a fixed request rate at checkout (and a small share of direct inventory
reads) so dashboards and alert rules always have a baseline to compare against.
Deliberately uninstrumented: it plays the role of external clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import signal
import sys
from collections import Counter

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("demo_loadgen")


class LoadgenSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    checkout_url: str = Field(default="http://checkout:8000", validation_alias="CHECKOUT_URL")
    inventory_url: str = Field(default="http://inventory:8000", validation_alias="INVENTORY_URL")
    rps: float = Field(default=8.0, gt=0, le=200, validation_alias="LOADGEN_RPS")
    inventory_read_share: float = Field(
        default=0.2, ge=0, le=1, validation_alias="LOADGEN_INVENTORY_READ_SHARE"
    )
    report_every_s: float = Field(default=30.0, gt=0, validation_alias="LOADGEN_REPORT_EVERY_S")


def pick_sku(rng: random.Random) -> str:
    return f"SKU-{rng.randint(1, 20):03d}"


async def one_request(
    client: httpx.AsyncClient, cfg: LoadgenSettings, rng: random.Random, stats: Counter[str]
) -> None:
    sku = pick_sku(rng)
    try:
        if rng.random() < cfg.inventory_read_share:
            resp = await client.get(f"{cfg.inventory_url}/inventory/{sku}")
        else:
            resp = await client.post(
                f"{cfg.checkout_url}/checkout", json={"sku": sku, "quantity": rng.randint(1, 3)}
            )
        stats[f"{resp.status_code // 100}xx"] += 1
    except httpx.HTTPError:
        stats["transport_error"] += 1


async def run(cfg: LoadgenSettings, stop: asyncio.Event) -> None:
    rng = random.Random()  # noqa: S311 - traffic shaping, not security
    stats: Counter[str] = Counter()
    interval = 1.0 / cfg.rps
    in_flight: set[asyncio.Task[None]] = set()
    loop = asyncio.get_running_loop()
    next_report = loop.time() + cfg.report_every_s

    async with httpx.AsyncClient(timeout=5.0) as client:
        while not stop.is_set():
            task = asyncio.create_task(one_request(client, cfg, rng, stats))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
            if loop.time() >= next_report:
                logger.info(json.dumps({"msg": "loadgen stats", **stats}))
                stats.clear()
                next_report = loop.time() + cfg.report_every_s
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        if in_flight:
            await asyncio.wait(in_flight, timeout=5.0)


def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    # httpx logs every request at INFO; periodic stats are enough.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = LoadgenSettings()
    logger.info(json.dumps({"msg": "loadgen starting", **cfg.model_dump()}))

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run(cfg, stop)

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
