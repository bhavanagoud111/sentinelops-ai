"""Kubernetes read access for investigations (wired in Phase 4).

Returns ``None`` when no cluster is configured, in which case rollout and pod
evidence is recorded as "unavailable" rather than guessed.
"""

from __future__ import annotations

import os

from sentinel_investigator.tools import KubernetesReader


def kubernetes_reader_from_env() -> KubernetesReader | None:
    path = os.environ.get("K8S_READER_KUBECONFIG")
    if not path or not os.path.exists(path):
        return None
    from sentinel_telemetry.kubernetes import KubernetesReadClient

    return KubernetesReadClient.from_kubeconfig(path)
