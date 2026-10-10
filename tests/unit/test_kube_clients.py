"""Kubernetes client construction: kubeconfig file vs. in-cluster ServiceAccount."""

from __future__ import annotations

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
