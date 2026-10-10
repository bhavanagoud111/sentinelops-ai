"""Allowlisted runbooks (design spec section 11).

This catalog is the *only* set of actions SentinelOps can ever execute. Each
runbook has a typed, bounded parameter model; anything an agent proposes is
parsed through it, so a model cannot smuggle in extra fields, other namespaces
or arbitrary commands. There is deliberately no "run command" runbook.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DEMO_NAMESPACE = "sentinelops-demo"
DEMO_DEPLOYMENTS = frozenset({"checkout", "inventory", "payment", "worker"})

K8sName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
]
FlagName = Annotated[str, StringConstraints(pattern=r"^FEATURE_[A-Z0-9_]{1,48}$")]


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RunbookId(StrEnum):
    RESTART_DEPLOYMENT = "restart_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    DISABLE_FEATURE_FLAG = "disable_feature_flag"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: K8sName = DEMO_NAMESPACE
    deployment: K8sName


class RestartParams(_Params):
    action: Literal["restart_deployment"] = "restart_deployment"


class RollbackParams(_Params):
    action: Literal["rollback_deployment"] = "rollback_deployment"
    # None = previous revision. The policy engine verifies the target exists and differs.
    to_revision: Annotated[int, Field(ge=1)] | None = None


class ScaleParams(_Params):
    action: Literal["scale_deployment"] = "scale_deployment"
    replicas: Annotated[int, Field(ge=1, le=10)]


class DisableFlagParams(_Params):
    action: Literal["disable_feature_flag"] = "disable_feature_flag"
    flag: FlagName


RunbookParams = RestartParams | RollbackParams | ScaleParams | DisableFlagParams


class Runbook(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: RunbookId
    title: str
    description: str
    risk: RiskLevel
    params_model: type[BaseModel]
    allowed_namespaces: frozenset[str]
    allowed_deployments: frozenset[str]
    scale_bounds: tuple[int, int] | None = None
    cooldown_s: int = 300
    version: int = 1


CATALOG: dict[RunbookId, Runbook] = {
    rb.id: rb
    for rb in (
        Runbook(
            id=RunbookId.RESTART_DEPLOYMENT,
            title="Restart deployment",
            description="Rolling restart (kubectl rollout restart equivalent). Mitigates leaks "
            "and stuck processes; does not fix bad code or config.",
            risk=RiskLevel.LOW,
            params_model=RestartParams,
            allowed_namespaces=frozenset({DEMO_NAMESPACE}),
            allowed_deployments=DEMO_DEPLOYMENTS,
        ),
        Runbook(
            id=RunbookId.ROLLBACK_DEPLOYMENT,
            title="Roll back deployment",
            description="Restore the pod template of the previous (or a named) ReplicaSet "
            "revision. Fixes bad image/config rollouts.",
            risk=RiskLevel.MEDIUM,
            params_model=RollbackParams,
            allowed_namespaces=frozenset({DEMO_NAMESPACE}),
            allowed_deployments=DEMO_DEPLOYMENTS,
        ),
        Runbook(
            id=RunbookId.SCALE_DEPLOYMENT,
            title="Scale deployment",
            description="Set replicas within predefined bounds.",
            risk=RiskLevel.MEDIUM,
            params_model=ScaleParams,
            allowed_namespaces=frozenset({DEMO_NAMESPACE}),
            allowed_deployments=DEMO_DEPLOYMENTS,
            scale_bounds=(1, 4),
        ),
        Runbook(
            id=RunbookId.DISABLE_FEATURE_FLAG,
            title="Disable feature flag",
            description="Set an allowlisted FEATURE_* environment flag to 'off' on a demo "
            "deployment (triggers a rolling update).",
            risk=RiskLevel.LOW,
            params_model=DisableFlagParams,
            allowed_namespaces=frozenset({DEMO_NAMESPACE}),
            allowed_deployments=DEMO_DEPLOYMENTS,
        ),
    )
}

ALLOWED_FLAGS = frozenset({"FEATURE_NEW_STOCK_INDEX", "FEATURE_PAYMENT_RETRY_STORM"})


def parse_params(runbook_id: str, raw: dict[str, object]) -> RunbookParams:
    """Validate agent/user-supplied parameters against the runbook's model.

    Raises KeyError for unknown runbooks and pydantic.ValidationError for bad params.
    """
    runbook = CATALOG[RunbookId(runbook_id)]
    data = {**raw, "action": runbook.id.value}
    parsed = runbook.params_model.model_validate(data)
    return parsed  # type: ignore[return-value]


def describe_catalog() -> list[dict[str, object]]:
    """Catalog summary given to agents (ids, purpose, parameters) — never credentials."""
    return [
        {
            "id": rb.id.value,
            "title": rb.title,
            "description": rb.description,
            "risk": rb.risk.value,
            "parameters": {
                k: v
                for k, v in rb.params_model.model_json_schema().get("properties", {}).items()
                if k != "action"
            },
        }
        for rb in CATALOG.values()
    ]
