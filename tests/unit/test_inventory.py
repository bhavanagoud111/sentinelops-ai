from __future__ import annotations

import time

import pytest
from demo_faultkit import FaultRegistry, FaultSpec
from demo_inventory.app import FAULT_DEPENDENCY_DELAY, create_app
from fastapi.testclient import TestClient


@pytest.fixture
def faults() -> FaultRegistry:
    return FaultRegistry([FAULT_DEPENDENCY_DELAY])


@pytest.fixture
def client(faults: FaultRegistry) -> TestClient:
    return TestClient(create_app(faults=faults))


def test_get_stock(client: TestClient) -> None:
    resp = client.get("/inventory/SKU-001")
    assert resp.status_code == 200
    assert resp.json()["available"] > 0


def test_unknown_sku(client: TestClient) -> None:
    assert client.get("/inventory/SKU-999").status_code == 404
    assert (
        client.post("/inventory/reserve", json={"sku": "SKU-999", "quantity": 1}).status_code == 404
    )


def test_reserve_decrements(client: TestClient) -> None:
    before = client.get("/inventory/SKU-002").json()["available"]
    resp = client.post("/inventory/reserve", json={"sku": "SKU-002", "quantity": 3})
    assert resp.status_code == 200
    assert resp.json()["remaining"] == before - 3


def test_insufficient_stock(client: TestClient) -> None:
    client.app.state.stock._levels["SKU-003"] = 1  # type: ignore[attr-defined]
    resp = client.post("/inventory/reserve", json={"sku": "SKU-003", "quantity": 2})
    assert resp.status_code == 409


def test_bad_sku_format_rejected(client: TestClient) -> None:
    assert client.get("/inventory/not-a-sku").status_code == 422


def test_dependency_delay_fault(client: TestClient, faults: FaultRegistry) -> None:
    faults.activate(FAULT_DEPENDENCY_DELAY, FaultSpec(delay_ms=150))
    start = time.perf_counter()
    assert client.get("/inventory/SKU-001").status_code == 200
    assert time.perf_counter() - start >= 0.15
