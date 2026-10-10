"""Pipeline load test: gateway throughput and end-to-end latency, measured.

    uv run python scripts/loadtest.py --rps 15 --seconds 60 --alerts-per-webhook 10

Sends Alertmanager-shaped webhooks (batches of alerts, like Alertmanager does) for
``--keys`` distinct correlation keys with repeated occurrences and 10% exact resends,
while concurrently reading the incidents API. Then reads timestamps from PostgreSQL:
  * gateway latency (client-side) p50/p95/p99 and status codes
  * alert received → incident created (correlator), per new incident
  * outbox row created → published (relay lag)
  * API GET /incidents latency under load
Writes docs/evaluation/load-<stamp>.json and .md. Uses service "loadtest" so the
investigator's queries return quickly and no demo workload is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
INGEST = "http://127.0.0.1:18001/api/v1/ingest/alertmanager"
API = "http://127.0.0.1:18000"
PG = os.environ.get("PG_DSN", "postgresql://sentinel:sentinel@127.0.0.1:15432/sentinelops")


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    return vs[min(len(vs) - 1, int(q * (len(vs) - 1) + 0.5))]


def webhook(run: str, keys: int, n: int, rng: random.Random) -> dict[str, Any]:
    alerts = []
    for _ in range(n):
        k = rng.randrange(keys)
        occurrence = rng.randrange(3)  # each key fires up to 3 distinct times
        alerts.append(
            {
                "status": "firing",
                "labels": {
                    "alertname": f"Load{run}K{k}",
                    "service": "loadtest",
                    "severity": "warning",
                },
                "annotations": {"summary": f"load test key {k}"},
                "startsAt": f"2026-10-10T00:0{occurrence}:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "",
                "fingerprint": f"{run}{k:06d}",
            }
        )
    return {
        "version": "4",
        "status": "firing",
        "receiver": "load",
        "groupKey": run,
        "alerts": alerts,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rps", type=float, default=15)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--alerts-per-webhook", type=int, default=10)
    ap.add_argument("--keys", type=int, default=150)
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    rng = random.Random(42)
    token = (ROOT / ".cache" / "secrets" / "ingest_token").read_text().strip()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{API}/api/v1/auth/dev-token", json={"username": "vera.viewer"})
        api_token = r.json()["access_token"]
    lat: list[float] = []
    codes: dict[int, int] = {}
    api_lat: list[float] = []
    sem = asyncio.Semaphore(args.concurrency)
    started = datetime.now(UTC)

    async with (
        httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"}) as client,
        httpx.AsyncClient(
            base_url=API, timeout=10, headers={"Authorization": f"Bearer {api_token}"}
        ) as api,
    ):
        stop = asyncio.Event()

        async def reader() -> None:
            while not stop.is_set():
                t0 = time.perf_counter()
                r = await api.get("/api/v1/incidents", params={"limit": 50})
                if r.status_code == 200:
                    api_lat.append(time.perf_counter() - t0)
                await asyncio.sleep(0.2)

        async def send(body: dict[str, Any]) -> None:
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await client.post(INGEST, json=body)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    lat.append(time.perf_counter() - t0)
                except httpx.HTTPError:
                    codes[0] = codes.get(0, 0) + 1

        rtask = asyncio.create_task(reader())
        tasks = []
        t_end = time.monotonic() + args.seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < t_end:
            body = (
                last
                if (last and rng.random() < 0.1)
                else webhook(run, args.keys, args.alerts_per_webhook, rng)
            )
            last = body
            tasks.append(asyncio.create_task(send(body)))
            await asyncio.sleep(1 / args.rps)
        await asyncio.gather(*tasks)
        await asyncio.sleep(10)  # let relay + correlator drain
        stop.set()
        await rtask

    conn = await asyncpg.connect(PG)
    try:
        corr = await conn.fetch(
            """SELECT EXTRACT(EPOCH FROM (i.created_at - MIN(a.received_at))) AS s
               FROM incidents i JOIN alert_events a ON a.incident_id = i.id
               WHERE i.fingerprint LIKE $1 GROUP BY i.id, i.created_at""",
            f"local:loadtest:Load{run}%",
        )
        relay = await conn.fetch(
            """SELECT EXTRACT(EPOCH FROM (published_at - created_at)) AS s FROM outbox_events
               WHERE created_at >= $1 AND published_at IS NOT NULL""",
            started,
        )
        counts = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM alert_events WHERE fingerprint LIKE $1) AS events,
                      (SELECT count(*) FROM incidents WHERE fingerprint LIKE $1) AS incidents,
                      (SELECT count(*) FROM outbox_events WHERE created_at >= $2
                         AND published_at IS NULL) AS unpublished""",
            f"local:loadtest:Load{run}%",
            started,
        )
    finally:
        await conn.close()

    e2e = [float(r["s"]) for r in corr if r["s"] is not None]
    rl = [float(r["s"]) for r in relay if r["s"] is not None]
    sent_webhooks = sum(codes.values())
    result = {
        "run": run,
        "started_at": started.isoformat(),
        "config": vars(args),
        "environment": {
            "host": platform.platform(),
            "cpu_count": os.cpu_count(),
            "note": "single Docker Desktop VM (10 CPU, 8 GB) shared by the full stack",
        },
        "webhooks_sent": sent_webhooks,
        "alerts_sent": sent_webhooks * args.alerts_per_webhook,
        "status_codes": codes,
        "gateway_latency_s": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "p99": pct(lat, 0.99)},
        "api_list_latency_s": {
            "p50": pct(api_lat, 0.5),
            "p95": pct(api_lat, 0.95),
            "n": len(api_lat),
        },
        "stored_alert_events": counts["events"],
        "incidents_created": counts["incidents"],
        "alert_to_incident_s": {
            "p50": pct(e2e, 0.5),
            "p95": pct(e2e, 0.95),
            "max": max(e2e, default=None),
            "n": len(e2e),
        },
        "outbox_publish_lag_s": {"p50": pct(rl, 0.5), "p95": pct(rl, 0.95), "n": len(rl)},
        "unpublished_after_drain": counts["unpublished"],
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "docs" / "evaluation" / f"load-{stamp}.json"
    out.write_text(json.dumps(result, indent=1, default=str))

    def ms(v: float | None) -> str:
        return "—" if v is None else f"{v * 1000:.0f} ms"

    md = f"""# Load test {stamp}

Config: {args.rps} webhooks/s × {args.alerts_per_webhook} alerts for {args.seconds:.0f}s,
{args.keys} correlation keys (≤3 occurrences each), 10% exact resends, {args.concurrency}
concurrent senders, concurrent API reads every 200 ms. Environment: {result["environment"]["note"]}.

| Measure | Result |
|---|---|
| Webhooks sent / alerts | {sent_webhooks} / {result["alerts_sent"]} |
| Status codes | {codes} |
| Gateway latency p50 / p95 / p99 | {ms(result["gateway_latency_s"]["p50"])} / {ms(result["gateway_latency_s"]["p95"])} / {ms(result["gateway_latency_s"]["p99"])} |
| Stored (deduplicated) alert events | {counts["events"]} |
| Incidents created (expected ≤ {args.keys}) | {counts["incidents"]} |
| Alert received → incident created p50 / p95 / max | {ms(result["alert_to_incident_s"]["p50"])} / {ms(result["alert_to_incident_s"]["p95"])} / {ms(result["alert_to_incident_s"]["max"])} |
| Outbox publish lag p50 / p95 | {ms(result["outbox_publish_lag_s"]["p50"])} / {ms(result["outbox_publish_lag_s"]["p95"])} |
| Unpublished outbox rows after 10 s drain | {counts["unpublished"]} |
| API GET /incidents p50 / p95 (n={len(api_lat)}) | {ms(result["api_list_latency_s"]["p50"])} / {ms(result["api_list_latency_s"]["p95"])} |

Raw: `{out.name}`. All values measured from client timings and PostgreSQL timestamps.
"""
    out.with_suffix(".md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
