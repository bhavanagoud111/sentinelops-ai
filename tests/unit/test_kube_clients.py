"""Kubernetes clients (kubeconfig vs. in-cluster) and the live evidence they feed."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client, config
from sentinel_executor.k8s_actions import DeploymentActions
from sentinel_investigator.k8s import kubernetes_reader_from_env
from sentinel_telemetry import kubernetes as tk


@pytest.fixture
def incluster(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def fake_load(client_configuration: Any = None, **_: Any) -> None:
        client_configuration.host = "https://10.96.0.1:443"
        calls.append(client_configuration)

    monkeypatch.setattr(config, "load_incluster_config", fake_load)
    return calls


def test_in_cluster_uses_service_account(incluster: list[Any]) -> None:
    api = tk.api_client(tk.IN_CLUSTER)
    assert isinstance(api, client.ApiClient)
    assert api.configuration.host == "https://10.96.0.1:443"
    assert len(incluster) == 1


def test_kubeconfig_path_does_not_touch_in_cluster(
    incluster: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_new(config_file: str) -> str:
        seen.append(config_file)
        return "api"

    monkeypatch.setattr(config, "new_client_from_config", fake_new)
    assert tk.api_client("/run/secrets/reader_kubeconfig") == "api"
    assert seen == ["/run/secrets/reader_kubeconfig"]
    assert incluster == []


def test_executor_in_cluster(incluster: list[Any]) -> None:
    actions = DeploymentActions.from_kubeconfig("in-cluster")
    assert actions.apps.api_client.configuration.host == "https://10.96.0.1:443"


def test_reader_from_env(
    incluster: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("K8S_READER_KUBECONFIG", raising=False)
    assert kubernetes_reader_from_env() is None
    monkeypatch.setenv("K8S_READER_KUBECONFIG", str(tmp_path / "missing"))
    assert kubernetes_reader_from_env() is None
    monkeypatch.setenv("K8S_READER_KUBECONFIG", "in-cluster")
    assert kubernetes_reader_from_env() is not None
    assert len(incluster) == 1


def _rs(created: str, *updates: tuple[str, str | None]) -> Any:
    from types import SimpleNamespace

    ts = datetime.fromisoformat
    fields = [SimpleNamespace(time=ts(t), subresource=sub) for t, sub in updates]
    return SimpleNamespace(
        metadata=SimpleNamespace(creation_timestamp=ts(created), managed_fields=fields)
    )


def test_reused_replicaset_reports_reactivation_time() -> None:
    # Re-deploying a template seen before reuses its ReplicaSet: created long ago,
    # re-activated (spec update) just now. Status-subresource writes don't count.
    rs = _rs(
        "2026-10-10T15:32:25+00:00",
        ("2026-10-10T16:05:18+00:00", None),
        ("2026-10-10T16:09:00+00:00", "status"),
    )
    assert tk.rolled_out_at(rs, current=True) == datetime.fromisoformat("2026-10-10T16:05:18+00:00")


def test_older_revision_keeps_creation_time() -> None:
    # Its last spec update is the scale-down caused by the next rollout.
    rs = _rs("2026-10-10T15:32:25+00:00", ("2026-10-10T16:05:18+00:00", None))
    assert tk.rolled_out_at(rs, current=False) == datetime.fromisoformat(
        "2026-10-10T15:32:25+00:00"
    )


def test_rolled_out_at_without_managed_fields() -> None:
    rs = _rs("2026-10-10T15:32:25+00:00")
    rs.metadata.managed_fields = None
    assert tk.rolled_out_at(rs, current=True) == datetime.fromisoformat("2026-10-10T15:32:25+00:00")


class _ReusedRollout:
    """A rollout re-activated a ReplicaSet created before the investigation window."""

    async def rollouts(self, namespace: str, deployment: str) -> dict[str, Any]:
        return {
            "deployment": deployment,
            "current_revision": 12,
            "revisions": [
                {"revision": 11, "created_at": "2026-10-10T15:30:47+00:00", "change": "x"},
                {
                    "revision": 12,
                    "created_at": "2026-10-10T15:32:25+00:00",
                    "rolled_out_at": "2026-10-10T16:03:00+00:00",
                    "change": "env PAYMENT_PROVIDER_URL: ok -> bad",
                },
            ],
            "current_env": {},
        }

    async def pod_status(self, namespace: str, deployment: str) -> dict[str, Any]:
        return {}


async def test_toolbox_counts_reused_replicaset_as_recent_rollout() -> None:
    import uuid

    from sentinel_investigator.models import EvidenceKind
    from sentinel_investigator.tools import LiveToolbox, ToolContext

    box = LiveToolbox(None, None, None, _ReusedRollout())  # type: ignore[arg-type]
    ctx = ToolContext(
        incident_id=uuid.uuid4(),
        scope=frozenset({"checkout"}),
        start=datetime.fromisoformat("2026-10-10T15:45:00+00:00"),
        end=datetime.fromisoformat("2026-10-10T16:05:00+00:00"),
    )
    ev = await box.collect(EvidenceKind.ROLLOUTS, "checkout", ctx)
    assert ev.anomalous
    assert [r["revision"] for r in ev.signals["recent_changes"]] == [12]


class _LeakProm:
    """Prometheus fake: one worker process leaking from 61 MiB to 531 MiB."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query_range(self, q: str, *_: Any) -> list[dict[str, Any]]:
        self.queries.append(q)
        mib = [61, 61, 61, 61, 174, 323, 473, 531, 531]
        return [{"values": [[1_760_000_000 + 60 * i, str(v * 2**20)] for i, v in enumerate(mib)]}]


async def test_memory_evidence_takes_max_across_processes() -> None:
    import uuid

    from sentinel_investigator.models import EvidenceKind
    from sentinel_investigator.tools import LiveToolbox, ToolContext

    prom = _LeakProm()
    box = LiveToolbox(prom, None, None)  # type: ignore[arg-type]
    ctx = ToolContext(
        incident_id=uuid.uuid4(),
        scope=frozenset({"worker"}),
        start=datetime.fromisoformat("2026-10-10T15:46:00+00:00"),
        end=datetime.fromisoformat("2026-10-10T16:06:00+00:00"),
    )
    ev = await box.collect(EvidenceKind.PROCESS_MEMORY, "worker", ctx)
    assert prom.queries == ['max(process_resident_memory_bytes{service_name="worker"})']
    assert ev.anomalous
    assert ev.signals["active"]
