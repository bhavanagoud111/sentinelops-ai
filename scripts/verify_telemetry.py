"""Phase 1 exit-gate check: one fault produces a visible metric, log, trace and alert.

Assumes the stack is up (`make up`) and scenario A has been injected
(`make inject-fault`). Polls each backend until the signal appears or the
timeout expires, then prints a pass/fail table. Exit code 0 only if all pass.

Usage: uv run python scripts/verify_telemetry.py [--timeout 300]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

PROM = f"http://127.0.0.1:{os.environ.get('PROMETHEUS_PORT', '19090')}"
AM = f"http://127.0.0.1:{os.environ.get('ALERTMANAGER_PORT', '19093')}"
LOKI = f"http://127.0.0.1:{os.environ.get('LOKI_PORT', '13100')}"
TEMPO = f"http://127.0.0.1:{os.environ.get('TEMPO_PORT', '13200')}"

ALERT = "CheckoutHigh5xxRate"


@dataclass
class Check:
    name: str
    probe: Callable[[httpx.Client], str | None]  # returns evidence string when satisfied
    evidence: str | None = None


def metric_5xx(c: httpx.Client) -> str | None:
    q = 'service:http_errors_5xx:ratio1m{service_name="checkout"}'
    r = c.get(f"{PROM}/api/v1/query", params={"query": q}).json()
    for s in r.get("data", {}).get("result", []):
        value = float(s["value"][1])
        if value > 0:
            return f"checkout 5xx ratio = {value:.1%}"
    return None


def alert_firing(c: httpx.Client) -> str | None:
    r = c.get(f"{AM}/api/v2/alerts", params={"filter": f'alertname="{ALERT}"', "active": "true"})
    alerts = r.json()
    firing = [a for a in alerts if a.get("status", {}).get("state") == "active"]
    if firing:
        return f"{ALERT} active in Alertmanager since {firing[0]['startsAt']}"
    return None


def error_log(c: httpx.Client) -> str | None:
    now = time.time_ns()
    q = '{service_name="checkout"} |= "configuration error"'
    r = c.get(
        f"{LOKI}/loki/api/v1/query_range",
        params={"query": q, "start": now - 15 * 60 * 10**9, "end": now, "limit": 5},
    ).json()
    for stream in r.get("data", {}).get("result", []):
        for _ts, line in stream.get("values", []):
            meta = stream.get("stream", {})
            trace = meta.get("trace_id", "?")
            return f"log {line[:60]!r} (trace_id={trace})"
    return None


def error_trace(c: httpx.Client) -> str | None:
    end = int(time.time())
    q = '{resource.service.name="checkout" && status=error}'
    r = c.get(
        f"{TEMPO}/api/search", params={"q": q, "start": end - 15 * 60, "end": end, "limit": 5}
    ).json()
    traces = r.get("traces", [])
    if traces:
        t = traces[0]
        return f"trace {t['traceID']} root={t.get('rootTraceName', '?')}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    checks = [
        Check("metric", metric_5xx),
        Check("log", error_log),
        Check("trace", error_trace),
        Check("alert", alert_firing),
    ]
    deadline = time.monotonic() + args.timeout
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline and any(ch.evidence is None for ch in checks):
            for ch in checks:
                if ch.evidence is None:
                    try:
                        ch.evidence = ch.probe(client)
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        print(f"  .. {ch.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            pending = [ch.name for ch in checks if ch.evidence is None]
            if pending:
                print(f"  waiting for: {', '.join(pending)}", file=sys.stderr)
                time.sleep(10)

    ok = True
    for ch in checks:
        status = "PASS" if ch.evidence else "FAIL"
        ok &= ch.evidence is not None
        print(f"[{status}] {ch.name:<6} {ch.evidence or 'not observed before timeout'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
