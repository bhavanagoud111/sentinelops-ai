"""Demo-only fault injector (spec section 15).

Five fixed scenarios, each a fixed change to the sandbox namespace — no
parameters beyond the scenario id. Uses the ``sentinel-chaos`` ServiceAccount
(patch deployments in sentinelops-demo only) and the demo services' fault admin
API. Reachable only from the API over the internal network with a service token.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from kubernetes import client, config

logger = logging.getLogger("sentinel_faultinjector")
NS = "sentinelops-demo"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    expected_alert: str
    expected_outcome: str


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in (
        Scenario(
            "A",
            "Checkout bad config rollout",
            "Rolls out checkout with a malformed PAYMENT_PROVIDER_URL; every request fails.",
            "CheckoutHigh5xxRate",
            "rollback_deployment proposed",
        ),
        Scenario(
            "B",
            "Inventory feature-flag latency",
            "Rolls out inventory with FEATURE_NEW_STOCK_INDEX=on (+1.2 s per request).",
            "InventoryHighLatencyP95",
            "disable_feature_flag proposed",
        ),
        Scenario(
            "C",
            "Payment provider slowdown",
            "The payment provider responds in 2.5 s; checkout timeouts and retries cascade.",
            "CheckoutHigh5xxRate",
            "escalated: no allowlisted fix for an upstream outage",
        ),
        Scenario(
            "D",
            "Inventory crash loop",
            "Rolls out inventory with corrupt seed data; new pods crash-loop.",
            "DeploymentCrashLooping",
            "rollback_deployment proposed",
        ),
        Scenario(
            "E",
            "Worker memory leak",
            "The worker leaks ~2.5 MB/s until restarted.",
            "WorkerMemoryHigh",
            "restart_deployment proposed",
        ),
    )
}

BASELINE_ENV: dict[str, dict[str, str]] = {
    "checkout": {"PAYMENT_PROVIDER_URL": "http://payment:8000/v1"},
    "inventory": {"FEATURE_NEW_STOCK_INDEX": "off", "INVENTORY_STOCK_SEED": "default"},
}


class Cluster:
    def __init__(self, kubeconfig: str) -> None:
        api = config.new_client_from_config(config_file=kubeconfig)  # type: ignore[no-untyped-call]
        self.apps: Any = client.AppsV1Api(api)

    def _set_env(self, deployment: str, env: dict[str, str]) -> None:
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": deployment,
                                "env": [{"name": k, "value": v} for k, v in env.items()],
                            }
                        ]
                    }
                }
            }
        }
        self.apps.patch_namespaced_deployment(deployment, NS, body)

    def _restart(self, deployment: str) -> None:
        ts = datetime.now(UTC).isoformat()
        body = {
            "spec": {
                "template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": ts}}}
            }
        }
        self.apps.patch_namespaced_deployment(deployment, NS, body)

    async def set_env(self, deployment: str, env: dict[str, str]) -> None:
        await asyncio.to_thread(self._set_env, deployment, env)

    async def restart(self, deployment: str) -> None:
        await asyncio.to_thread(self._restart, deployment)


class DemoAdmin:
    def __init__(self, token: str, base: Callable[[str], str]) -> None:
        self.token = token
        self.base = base

    async def call(self, method: str, service: str, path: str, body: Any = None) -> None:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.request(
                method,
                f"{self.base(service)}{path}",
                json=body,
                headers={"X-Fault-Token": self.token},
            )
            r.raise_for_status()


def create_app(cluster: Cluster, admin: DemoAdmin, service_token: str) -> FastAPI:
    app = FastAPI(title="sentinel-faultinjector", version="0.1.0", docs_url=None, redoc_url=None)

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        _, _, tok = (authorization or "").partition(" ")
        if not hmac.compare_digest(tok.encode(), service_token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service token")

    guard = [Depends(auth)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/scenarios", dependencies=guard)
    def scenarios() -> list[dict[str, str]]:
        return [s.__dict__ for s in SCENARIOS.values()]

    @app.post("/scenarios/{scenario_id}", dependencies=guard)
    async def start(scenario_id: str) -> dict[str, str]:
        if scenario_id not in SCENARIOS:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown scenario")
        if scenario_id == "A":
            await cluster.set_env("checkout", {"PAYMENT_PROVIDER_URL": "payments.internal:::"})
        elif scenario_id == "B":
            await cluster.set_env("inventory", {"FEATURE_NEW_STOCK_INDEX": "on"})
        elif scenario_id == "C":
            await admin.call(
                "PUT", "payment", "/admin/faults/provider_slowdown", {"delay_ms": 2500}
            )
        elif scenario_id == "D":
            await cluster.set_env("inventory", {"INVENTORY_STOCK_SEED": "corrupt"})
        elif scenario_id == "E":
            await admin.call("PUT", "worker", "/admin/faults/memory_leak", {"delay_ms": 512})
        logger.warning("scenario started", extra={"scenario": scenario_id})
        return {"scenario": scenario_id, "status": "started"}

    @app.post("/reset", dependencies=guard)
    async def reset() -> dict[str, str]:
        for svc in ("payment", "worker"):
            try:
                await admin.call("DELETE", svc, "/admin/faults")
            except httpx.HTTPError as exc:
                logger.warning(
                    "could not clear runtime faults", extra={"service": svc, "error": str(exc)}
                )
        for deployment, env in BASELINE_ENV.items():
            await cluster.set_env(deployment, env)
        for deployment in ("payment", "worker"):
            await cluster.restart(deployment)
        return {"status": "baseline restored"}

    return app


def build() -> FastAPI:  # pragma: no cover - process entrypoint
    from sentinel_common import TelemetrySettings, setup_telemetry
    from sentinel_common.secrets import read_secret

    setup_telemetry(TelemetrySettings())
    node = os.environ.get("DEMO_NODE_HOST", "sentinelops-control-plane")
    ports = {"checkout": 30081, "inventory": 30082, "payment": 30083, "worker": 30084}
    return create_app(
        Cluster(os.environ["K8S_CHAOS_KUBECONFIG"]),
        DemoAdmin(os.environ.get("FAULT_ADMIN_TOKEN", ""), lambda s: f"http://{node}:{ports[s]}"),
        read_secret("FAULTINJECTOR_TOKEN") or "",
    )
