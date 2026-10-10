"""Remediation policy engine (design spec section 11). Pure functions, no I/O.

``evaluate`` runs *every* check and returns all results, so an operator sees the
full picture of why an action was allowed or blocked. An action is allowed only
if every check passes. The executor calls this immediately before writing to
the cluster (time-of-check ≈ time-of-use), with a fresh deployment snapshot.

The approval is bound to the exact action: ``request_hash`` covers incident,
runbook, normalized parameters and the deployment revision observed when the
approval was requested. Any drift (someone else deployed, params changed,
different incident) invalidates the approval.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from sentinel_policy.runbooks import (
    ALLOWED_FLAGS,
    CATALOG,
    DisableFlagParams,
    RollbackParams,
    RunbookId,
    RunbookParams,
    ScaleParams,
    parse_params,
)


class RemediationMode(StrEnum):
    DISABLED = "disabled"  # nothing can execute
    RECOMMEND_ONLY = "recommend_only"  # approvals allowed, execution blocked (default)
    SANDBOX = "sandbox"  # allowlisted writes to the demo namespace


@dataclass(frozen=True)
class ApprovalSnapshot:
    id: uuid.UUID
    incident_id: uuid.UUID
    runbook_id: str
    params: dict[str, Any]
    status: str  # pending | approved | rejected | expired | consumed
    requested_by: str
    approved_by: str | None
    expires_at: datetime
    request_hash: str
    target_revision: int | None


@dataclass(frozen=True)
class DeploymentSnapshot:
    namespace: str
    name: str
    exists: bool
    revision: int | None = None
    replicas: int | None = None
    revisions: tuple[int, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyContext:
    now: datetime
    mode: RemediationMode
    incident_id: uuid.UUID
    incident_status: str
    approval: ApprovalSnapshot | None
    deployment: DeploymentSnapshot | None
    active_executions_for_target: int
    last_success_at: datetime | None
    allowed_incident_statuses: frozenset[str] = frozenset({"AWAITING_APPROVAL", "REMEDIATING"})


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    checks: tuple[Check, ...]
    params: RunbookParams | None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


def normalize_params(runbook_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Canonical parameter dict (validated, defaults filled, no 'action')."""
    return parse_params(runbook_id, raw).model_dump(exclude={"action"})


def compute_request_hash(
    incident_id: uuid.UUID, runbook_id: str, params: dict[str, Any], target_revision: int | None
) -> str:
    body = json.dumps(
        {
            "incident_id": str(incident_id),
            "runbook_id": runbook_id,
            "params": normalize_params(runbook_id, params),
            "target_revision": target_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def evaluate(runbook_id: str, raw_params: dict[str, Any], ctx: PolicyContext) -> PolicyDecision:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(name, ok, detail))
        return ok

    add(
        "mode_allows_writes",
        ctx.mode is RemediationMode.SANDBOX,
        f"REMEDIATION_MODE={ctx.mode.value}",
    )

    rb_ok = add(
        "runbook_allowlisted", runbook_id in {r.value for r in RunbookId}, f"runbook {runbook_id!r}"
    )
    params: RunbookParams | None = None
    if rb_ok:
        try:
            params = parse_params(runbook_id, raw_params)
            add("params_valid", True, "parameters match the runbook schema")
        except ValidationError as exc:
            add("params_valid", False, f"{exc.error_count()} validation error(s)")
    else:
        add("params_valid", False, "runbook not allowlisted")

    rb = CATALOG.get(RunbookId(runbook_id)) if rb_ok else None
    if params is not None and rb is not None:
        add(
            "namespace_allowed",
            params.namespace in rb.allowed_namespaces,
            f"namespace {params.namespace!r}",
        )
        add(
            "deployment_allowed",
            params.deployment in rb.allowed_deployments,
            f"deployment {params.deployment!r}",
        )
    else:
        add("namespace_allowed", False, "no valid parameters")
        add("deployment_allowed", False, "no valid parameters")

    a = ctx.approval
    add("approval_present", a is not None, "approval record found" if a else "no approval")
    if a is not None:
        add("approval_approved", a.status == "approved", f"approval status {a.status!r}")
        add("approval_not_expired", ctx.now < a.expires_at, f"expires {a.expires_at.isoformat()}")
        add(
            "four_eyes",
            bool(a.approved_by) and a.approved_by != a.requested_by,
            f"requested by {a.requested_by!r}, approved by {a.approved_by!r}",
        )
        bound = (
            a.incident_id == ctx.incident_id
            and a.runbook_id == runbook_id
            and params is not None
            and a.request_hash
            == compute_request_hash(ctx.incident_id, runbook_id, raw_params, a.target_revision)
        )
        add(
            "approval_bound_to_action",
            bound,
            "approval hash matches incident, runbook, parameters and target revision"
            if bound
            else "approval does not match this exact action",
        )
    else:
        for name in (
            "approval_approved",
            "approval_not_expired",
            "four_eyes",
            "approval_bound_to_action",
        ):
            add(name, False, "no approval")

    add(
        "incident_state",
        ctx.incident_status in ctx.allowed_incident_statuses,
        f"incident is {ctx.incident_status}",
    )

    d = ctx.deployment
    add(
        "target_exists",
        d is not None and d.exists,
        f"deployment {d.namespace}/{d.name}" if d else "no snapshot",
    )
    if d is not None and d.exists:
        expected = a.target_revision if a else None
        add(
            "no_revision_drift",
            expected is not None and d.revision == expected,
            f"live revision {d.revision}, approved against {expected}",
        )
        if isinstance(params, RollbackParams):
            target = params.to_revision or max(
                (r for r in d.revisions if d.revision is None or r < d.revision), default=None
            )
            add(
                "rollback_target_available",
                target is not None and target in d.revisions and target != d.revision,
                f"rollback to revision {target} (available {list(d.revisions)})",
            )
        if isinstance(params, ScaleParams) and rb and rb.scale_bounds:
            lo, hi = rb.scale_bounds
            add(
                "scale_within_bounds",
                lo <= params.replicas <= hi,
                f"replicas {params.replicas} within [{lo}, {hi}]",
            )
        if isinstance(params, DisableFlagParams):
            add("flag_allowlisted", params.flag in ALLOWED_FLAGS, f"flag {params.flag}")
            add(
                "flag_currently_on",
                d.env.get(params.flag, "").lower() == "on",
                f"{params.flag}={d.env.get(params.flag, '<unset>')}",
            )
    else:
        add("no_revision_drift", False, "target missing")

    add(
        "no_concurrent_action",
        ctx.active_executions_for_target == 0,
        f"{ctx.active_executions_for_target} other action(s) in flight on this target",
    )
    cooldown = timedelta(seconds=rb.cooldown_s if rb else 300)
    add(
        "cooldown_elapsed",
        ctx.last_success_at is None or ctx.now - ctx.last_success_at >= cooldown,
        "no recent action"
        if ctx.last_success_at is None
        else f"last success {ctx.last_success_at.isoformat()}, cooldown {cooldown}",
    )

    allowed = all(c.passed for c in checks)
    return PolicyDecision(allowed=allowed, checks=tuple(checks), params=params if allowed else None)
