from __future__ import annotations

import random

import pytest
from demo_faultkit import FaultRegistry, FaultSpec, admin_router, mount_if_enabled
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "test-token"


def _client(registry: FaultRegistry) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router(registry, TOKEN))
    return TestClient(app)


class TestRegistry:
    def test_rejects_unknown_fault(self) -> None:
        reg = FaultRegistry(["a"])
        with pytest.raises(KeyError):
            reg.activate("b", FaultSpec())

    def test_activate_and_clear(self) -> None:
        reg = FaultRegistry(["a"])
        reg.activate("a", FaultSpec(delay_ms=10))
        assert reg.get("a") == FaultSpec(delay_ms=10)
        reg.clear("a")
        assert reg.get("a") is None

    def test_inactive_never_triggers(self) -> None:
        assert FaultRegistry(["a"]).triggered("a") is None

    def test_probability_respected(self) -> None:
        reg = FaultRegistry(["a"], rng=random.Random(42))
        reg.activate("a", FaultSpec(probability=0.5))
        fired = sum(reg.triggered("a") is not None for _ in range(2000))
        assert 850 < fired < 1150

    def test_spec_bounds(self) -> None:
        with pytest.raises(ValueError, match="less than or equal"):
            FaultSpec(probability=1.5)
        with pytest.raises(ValueError, match="less than or equal"):
            FaultSpec(delay_ms=60_000)


class TestAdminRouter:
    def test_requires_token(self) -> None:
        client = _client(FaultRegistry(["a"]))
        assert client.get("/admin/faults").status_code == 401
        assert client.get("/admin/faults", headers={"X-Fault-Token": "wrong"}).status_code == 401

    def test_activate_list_clear(self) -> None:
        reg = FaultRegistry(["a"])
        client = _client(reg)
        h = {"X-Fault-Token": TOKEN}
        assert (
            client.put("/admin/faults/a", json={"probability": 0.9}, headers=h).status_code == 204
        )
        body = client.get("/admin/faults", headers=h).json()
        assert body["active"]["a"]["probability"] == 0.9
        assert client.delete("/admin/faults/a", headers=h).status_code == 204
        assert reg.get("a") is None

    def test_unknown_fault_404(self) -> None:
        client = _client(FaultRegistry(["a"]))
        resp = client.put("/admin/faults/zzz", json={}, headers={"X-Fault-Token": TOKEN})
        assert resp.status_code == 404

    def test_empty_token_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            admin_router(FaultRegistry(["a"]), "")

    @pytest.mark.parametrize("token", [None, ""])
    def test_not_mounted_without_token(self, token: str | None) -> None:
        app = FastAPI()
        assert mount_if_enabled(app.include_router, FaultRegistry(["a"]), token) is False
        assert TestClient(app).get("/admin/faults").status_code == 404
