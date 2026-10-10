"""Bounded Kubernetes write operations for allowlisted runbooks.

Each runbook maps to exactly one fixed JSON-patch template against one
Deployment. There is no code path that accepts a command, a manifest or a
resource kind from input. Every patch is first sent with ``dryRun=All`` so the
API server validates it (admission, schema) before the real write.
The ServiceAccount behind this client can only get/list/patch deployments (and
read replicasets/pods) in the demo namespace.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from sentinel_policy import (
    DisableFlagParams,
    RestartParams,
    RollbackParams,
    RunbookParams,
    ScaleParams,
)
from sentinel_policy.engine import DeploymentSnapshot

REVISION = "deployment.kubernetes.io/revision"


@dataclass(frozen=True)
class ActionResult:
    command_template: str
    patch: list[dict[str, Any]]
    before: dict[str, Any]
    after: dict[str, Any]
    rollout_seconds: float


class RolloutTimeoutError(RuntimeError):
    pass


def _state(dep: Any) -> dict[str, Any]:
    c = dep.spec.template.spec.containers[0]
    return {
        "revision": int((dep.metadata.annotations or {}).get(REVISION, "0")),
        "generation": dep.metadata.generation,
        "replicas": dep.spec.replicas,
        "ready_replicas": dep.status.ready_replicas or 0,
        "image": c.image,
        "env": {e.name: e.value for e in (c.env or []) if e.value is not None},
    }


IN_CLUSTER = "in-cluster"  # use the pod's own ServiceAccount (Helm deployments)


class DeploymentActions:
    def __init__(self, api_client: Any) -> None:
        self.apps: Any = client.AppsV1Api(api_client)

    @classmethod
    def from_kubeconfig(cls, path: str) -> DeploymentActions:
        if path == IN_CLUSTER:
            cfg = client.Configuration()
            config.load_incluster_config(client_configuration=cfg)  # type: ignore[no-untyped-call]
            return cls(client.ApiClient(cfg))
        return cls(config.new_client_from_config(config_file=path))  # type: ignore[no-untyped-call]

    # -------------------------------------------------------------- read
    def _owned_replicasets(self, dep: Any) -> list[Any]:
        sel = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
        rss = self.apps.list_namespaced_replica_set(
            dep.metadata.namespace, label_selector=sel
        ).items
        return [
            rs
            for rs in rss
            if any(o.uid == dep.metadata.uid for o in (rs.metadata.owner_references or []))
        ]

    def _snapshot(self, namespace: str, name: str) -> DeploymentSnapshot:
        try:
            dep = self.apps.read_namespaced_deployment(name, namespace)
        except client.ApiException as exc:
            if exc.status == 404:
                return DeploymentSnapshot(namespace=namespace, name=name, exists=False)
            raise
        st = _state(dep)
        revs = sorted(
            int((rs.metadata.annotations or {}).get(REVISION, "0"))
            for rs in self._owned_replicasets(dep)
        )
        return DeploymentSnapshot(
            namespace=namespace,
            name=name,
            exists=True,
            revision=st["revision"],
            replicas=st["replicas"],
            revisions=tuple(revs),
            env=st["env"],
        )

    async def snapshot(self, namespace: str, name: str) -> DeploymentSnapshot:
        return await asyncio.to_thread(self._snapshot, namespace, name)

    # -------------------------------------------------------------- write
    def _build_patch(self, dep: Any, params: RunbookParams) -> tuple[str, list[dict[str, Any]]]:
        if isinstance(params, RestartParams):
            ts = datetime.now(UTC).isoformat()
            ann = dep.spec.template.metadata.annotations
            op = "replace" if ann else "add"
            value: Any = {**(ann or {}), "kubectl.kubernetes.io/restartedAt": ts}
            return (
                "kubectl rollout restart deployment/{deployment} -n {namespace}",
                [{"op": op, "path": "/spec/template/metadata/annotations", "value": value}],
            )
        if isinstance(params, ScaleParams):
            return (
                "kubectl scale deployment/{deployment} --replicas={replicas} -n {namespace}",
                [{"op": "replace", "path": "/spec/replicas", "value": params.replicas}],
            )
        if isinstance(params, DisableFlagParams):
            containers = dep.spec.template.spec.containers
            ci = next((i for i, c in enumerate(containers) if c.name == params.deployment), 0)
            env = containers[ci].env or []
            ei = next((i for i, e in enumerate(env) if e.name == params.flag), None)
            if ei is None:
                raise ValueError(f"{params.flag} is not set on {params.deployment}")
            return (
                "kubectl set env deployment/{deployment} {flag}=off -n {namespace}",
                [
                    {
                        "op": "replace",
                        "path": f"/spec/template/spec/containers/{ci}/env/{ei}/value",
                        "value": "off",
                    }
                ],
            )
        if isinstance(params, RollbackParams):
            current = int((dep.metadata.annotations or {}).get(REVISION, "0"))
            by_rev = {
                int((rs.metadata.annotations or {}).get(REVISION, "0")): rs
                for rs in self._owned_replicasets(dep)
            }
            target = params.to_revision or max((r for r in by_rev if r < current), default=None)
            if target is None or target not in by_rev:
                raise ValueError(f"no rollback target (current {current}, have {sorted(by_rev)})")
            template = self.apps.api_client.sanitize_for_serialization(by_rev[target].spec.template)
            template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
            return (
                "kubectl rollout undo deployment/{deployment} "
                f"--to-revision={target} -n {{namespace}}",
                [{"op": "replace", "path": "/spec/template", "value": template}],
            )
        raise TypeError(f"unsupported params {type(params).__name__}")

    def _apply(self, params: RunbookParams, rollout_timeout_s: float) -> ActionResult:
        ns, name = params.namespace, params.deployment
        dep = self.apps.read_namespaced_deployment(name, ns)
        before = _state(dep)
        template, patch = self._build_patch(dep, params)
        # Server-side validation first; nothing is persisted by a dry run.
        self.apps.patch_namespaced_deployment(name, ns, patch, dry_run="All")
        self.apps.patch_namespaced_deployment(name, ns, patch)
        t0 = time.monotonic()
        while True:
            d = self.apps.read_namespaced_deployment(name, ns)
            s, spec = d.status, d.spec
            done = (
                (s.observed_generation or 0) >= d.metadata.generation
                and (s.updated_replicas or 0) == spec.replicas
                and (s.available_replicas or 0) == spec.replicas
                and (s.replicas or 0) == spec.replicas
            )
            if done:
                break
            if time.monotonic() - t0 > rollout_timeout_s:
                raise RolloutTimeoutError(
                    f"rollout of {ns}/{name} not complete after {rollout_timeout_s:.0f}s"
                )
            time.sleep(2)
        return ActionResult(
            command_template=template.format(**params.model_dump()),
            patch=patch
            if not isinstance(params, RollbackParams)
            else [{"op": "replace", "path": "/spec/template", "value": "<revision template>"}],
            before=before,
            after=_state(d),
            rollout_seconds=round(time.monotonic() - t0, 1),
        )

    async def apply(self, params: RunbookParams, rollout_timeout_s: float = 180.0) -> ActionResult:
        return await asyncio.to_thread(self._apply, params, rollout_timeout_s)
