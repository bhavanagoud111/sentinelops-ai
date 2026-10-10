from __future__ import annotations

import time

from demo_faultkit import FaultRegistry, FaultSpec
from demo_payment.app import FAULT_PROVIDER_SLOWDOWN
from demo_payment.app import create_app as payment_app
from demo_worker import app as worker_mod
from fastapi.testclient import TestClient


def test_payment_authorizes() -> None:
    client = TestClient(payment_app())
    r = client.post("/v1/payments", json={"order_ref": "o1", "amount_cents": 100})
    assert r.status_code == 200
    assert r.json()["status"] == "authorized"


def test_payment_slowdown_fault() -> None:
    reg = FaultRegistry([FAULT_PROVIDER_SLOWDOWN])
    reg.activate(FAULT_PROVIDER_SLOWDOWN, FaultSpec(delay_ms=150))
    client = TestClient(payment_app(faults=reg))
    start = time.perf_counter()
    client.post("/v1/payments", json={"order_ref": "o1", "amount_cents": 100})
    assert time.perf_counter() - start >= 0.15


def test_worker_leak_grows_and_restart_clears() -> None:
    reg = FaultRegistry([worker_mod.FAULT_MEMORY_LEAK])
    reg.activate(worker_mod.FAULT_MEMORY_LEAK, FaultSpec(delay_ms=64))
    worker_mod._LEAK.clear()
    with TestClient(worker_mod.create_app(faults=reg, interval_s=0.01)) as client:
        time.sleep(0.2)
        body = client.get("/healthz").json()
    assert body["leaked_chunks"] > 0
    assert body["rss_bytes"] > 0
    worker_mod._LEAK.clear()  # a restart starts with an empty cache
