"""Read-only Kubernetes view for investigations.

Uses a kubeconfig bound to a ServiceAccount that can only get/list deployments,
replicasets and pods in the demo namespace (see infra/kind/rbac.yaml). The
official client is synchronous, so calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from kubernetes import client, config

REVISION = "deployment.kubernetes.io/revision"

# Kubeconfig "path" meaning: use the pod's own ServiceAccount (Helm deployments).
IN_CLUSTER = "in-cluster"


def api_client(source: str) -> Any:
    """API client from a kubeconfig file, or from the pod ServiceAccount for ``in-cluster``."""
    if source == IN_CLUSTER:
        cfg = client.Configuration()
        config.load_incluster_config(client_configuration=cfg)  # type: ignore[no-untyped-call]
        return client.ApiClient(cfg)
    return config.new_client_from_config(config_file=source)  # type: ignore[no-untyped-call]


def _env(template: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in template.spec.containers or []:
        for e in c.env or []:
            if e.value is not None:
                out[f"{e.name}"] = e.value
    return out


def _image(template: Any) -> str:
    return ",".join(c.image for c in template.spec.containers or [])


def describe_change(prev: Any | None, cur: Any) -> str:
    """Human-readable diff between two ReplicaSet pod templates (env + image only)."""
    if prev is None:
        return "initial revision"
    parts: list[str] = []
    a, b = _env(prev.spec.template), _env(cur.spec.template)
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            parts.append(f"env {k}: {a.get(k, '<unset>')} -> {b.get(k, '<unset>')}")
    ia, ib = _image(prev.spec.template), _image(cur.spec.template)
    if ia != ib:
        parts.append(f"image: {ia} -> {ib}")
    return "; ".join(parts) or "pod template annotations only (e.g. restart)"


class KubernetesReadClient:
    def __init__(self, api_client: Any) -> None:
        # The generated models are Optional-heavy; we treat API objects as Any and
        # guard with ``or`` defaults instead.
        self.apps: Any = client.AppsV1Api(api_client)
        self.core: Any = client.CoreV1Api(api_client)

    @classmethod
    def from_kubeconfig(cls, path: str) -> KubernetesReadClient:
        return cls(api_client(path))

    def _replicasets(self, namespace: str, deployment: str) -> tuple[Any, list[Any]]:
        dep = self.apps.read_namespaced_deployment(deployment, namespace)
        selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
        rss = self.apps.list_namespaced_replica_set(namespace, label_selector=selector).items
        owned = [
            rs
            for rs in rss
            if any(o.uid == dep.metadata.uid for o in (rs.metadata.owner_references or []))
        ]
        owned.sort(key=lambda rs: int((rs.metadata.annotations or {}).get(REVISION, "0")))
        return dep, owned

    def _rollouts(self, namespace: str, deployment: str) -> dict[str, Any]:
        dep, rss = self._replicasets(namespace, deployment)
        revisions = []
        prev = None
        for rs in rss:
            created: datetime = rs.metadata.creation_timestamp
            revisions.append(
                {
                    "revision": int((rs.metadata.annotations or {}).get(REVISION, "0")),
                    "created_at": created.isoformat() if created else None,
                    "image": _image(rs.spec.template),
                    "replicas": rs.status.replicas or 0,
                    "change": describe_change(prev, rs),
                }
            )
            prev = rs
        live_env = _env(dep.spec.template)
        return {
            "deployment": deployment,
            "current_revision": int((dep.metadata.annotations or {}).get(REVISION, "0")),
            "revisions": revisions[-10:],
            # Feature flags as deployed right now (a change may since have been reverted).
            "current_env": {k: v for k, v in live_env.items() if k.startswith("FEATURE_")},
        }

    def _pod_status(self, namespace: str, deployment: str) -> dict[str, Any]:
        dep = self.apps.read_namespaced_deployment(deployment, namespace)
        selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
        pods = self.core.list_namespaced_pod(namespace, label_selector=selector).items
        restarts = 0
        waiting: set[str] = set()
        terminated: set[str] = set()
        ready = 0
        crash_looping = False
        oom_killed = False
        per_pod: list[dict[str, Any]] = []
        for p in pods:
            statuses = p.status.container_statuses or [] if p.status else []
            if statuses and all(cs.ready for cs in statuses):
                ready += 1
            for cs in statuses:
                n = cs.restart_count or 0
                restarts += n
                w = cs.state.waiting.reason if cs.state and cs.state.waiting else None
                last = (
                    cs.last_state.terminated if cs.last_state and cs.last_state.terminated else None
                )
                if w:
                    waiting.add(w)
                if last and last.reason:
                    terminated.add(last.reason)
                # Kubernetes cycles Running -> Error -> CrashLoopBackOff; a single sample can
                # land in any phase, so judge by restart count and the last termination too.
                if n >= 2 and (
                    w == "CrashLoopBackOff" or (last and last.exit_code not in (0, None))
                ):
                    crash_looping = True
                if last and last.reason == "OOMKilled":
                    oom_killed = True
                per_pod.append(
                    {
                        "pod": p.metadata.name if p.metadata else "?",
                        "ready": bool(cs.ready),
                        "restarts": n,
                        "waiting": w,
                        "last_terminated": last.reason if last else None,
                        "last_exit_code": last.exit_code if last else None,
                    }
                )
        return {
            "desired": dep.spec.replicas or 0,
            "ready": ready,
            "pods": len(pods),
            "restarts": restarts,
            "waiting_reasons": sorted(waiting),
            "last_terminated_reasons": sorted(terminated),
            "crash_looping": crash_looping,
            "oom_killed": oom_killed,
            "containers": per_pod[:10],
            "anomalous": crash_looping or oom_killed or "ImagePullBackOff" in waiting,
        }

    async def rollouts(self, namespace: str, deployment: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._rollouts, namespace, deployment)

    async def pod_status(self, namespace: str, deployment: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._pod_status, namespace, deployment)
