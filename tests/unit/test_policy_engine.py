"""Policy engine: every way an action can be unsafe must be blocked (spec §11, gate P4)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sentinel_policy import describe_catalog, parse_params
from sentinel_policy.engine import (
    ApprovalSnapshot,
    DeploymentSnapshot,
    PolicyContext,
    RemediationMode,
    compute_request_hash,
    evaluate,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
INCIDENT = uuid.uuid4()
PARAMS: dict[str, Any] = {"deployment": "checkout"}


def approval(**kw: Any) -> ApprovalSnapshot:
    runbook = kw.pop("runbook_id", "rollback_deployment")
    params = kw.pop("params", PARAMS)
    rev = kw.pop("target_revision", 3)
    base = ApprovalSnapshot(
        id=uuid.uuid4(),
        incident_id=INCIDENT,
        runbook_id=runbook,
        params=params,
        status="approved",
        requested_by="oscar.operator",
        approved_by="anna.approver",
        expires_at=NOW + timedelta(minutes=10),
        request_hash=compute_request_hash(INCIDENT, runbook, params, rev),
        target_revision=rev,
    )
    return replace(base, **kw)


def ctx(**kw: Any) -> PolicyContext:
    base = PolicyContext(
        now=NOW,
        mode=RemediationMode.SANDBOX,
        incident_id=INCIDENT,
        incident_status="REMEDIATING",
        approval=approval(),
        deployment=DeploymentSnapshot(
            namespace="sentinelops-demo",
            name="checkout",
            exists=True,
            revision=3,
            replicas=2,
            revisions=(1, 2, 3),
            env={"FEATURE_NEW_STOCK_INDEX": "on"},
        ),
        active_executions_for_target=0,
        last_success_at=None,
    )
    return replace(base, **kw)


def failed(decision: Any) -> set[str]:
    return {c.name for c in decision.failed}


def test_fully_valid_action_is_allowed() -> None:
    d = evaluate("rollback_deployment", PARAMS, ctx())
    assert d.allowed, d.failed
    assert d.params is not None


@pytest.mark.parametrize(
    ("override", "expected_failure"),
    [
        ({"mode": RemediationMode.RECOMMEND_ONLY}, "mode_allows_writes"),
        ({"mode": RemediationMode.DISABLED}, "mode_allows_writes"),
        ({"approval": None}, "approval_present"),
        ({"approval": approval(status="rejected")}, "approval_approved"),
        ({"approval": approval(status="pending", approved_by=None)}, "approval_approved"),
        ({"approval": approval(status="consumed")}, "approval_approved"),
        ({"approval": approval(expires_at=NOW - timedelta(seconds=1))}, "approval_not_expired"),
        ({"approval": approval(approved_by="oscar.operator")}, "four_eyes"),
        ({"approval": approval(incident_id=uuid.uuid4())}, "approval_bound_to_action"),
        ({"approval": approval(request_hash="0" * 64)}, "approval_bound_to_action"),
        ({"incident_status": "RESOLVED"}, "incident_state"),
        ({"incident_status": "REJECTED"}, "incident_state"),
        ({"active_executions_for_target": 1}, "no_concurrent_action"),
        ({"last_success_at": NOW - timedelta(seconds=30)}, "cooldown_elapsed"),
    ],
)
def test_blocked(override: dict[str, Any], expected_failure: str) -> None:
    d = evaluate("rollback_deployment", PARAMS, ctx(**override))
    assert not d.allowed
    assert expected_failure in failed(d)
    assert d.params is None


def test_revision_drift_blocks() -> None:
    dep = DeploymentSnapshot(
        namespace="sentinelops-demo",
        name="checkout",
        exists=True,
        revision=4,
        revisions=(1, 2, 3, 4),
    )
    d = evaluate("rollback_deployment", PARAMS, ctx(deployment=dep))
    assert "no_revision_drift" in failed(d)


def test_approval_for_other_params_cannot_be_reused() -> None:
    # Approved a rollback of checkout; attempt a rollback of inventory with the same approval.
    d = evaluate("rollback_deployment", {"deployment": "inventory"}, ctx())
    assert "approval_bound_to_action" in failed(d)


def test_approval_for_other_runbook_cannot_be_reused() -> None:
    d = evaluate("restart_deployment", PARAMS, ctx())
    assert "approval_bound_to_action" in failed(d)


@pytest.mark.parametrize(
    "params",
    [
        {"deployment": "checkout", "namespace": "kube-system"},
        {"deployment": "checkout", "namespace": "default"},
    ],
)
def test_cross_namespace_blocked(params: dict[str, Any]) -> None:
    a = approval(params=params)
    d = evaluate("rollback_deployment", params, ctx(approval=a))
    assert "namespace_allowed" in failed(d)


def test_unknown_deployment_blocked() -> None:
    params = {"deployment": "coredns"}
    d = evaluate("rollback_deployment", params, ctx(approval=approval(params=params)))
    assert "deployment_allowed" in failed(d)


def test_non_allowlisted_runbook_blocked() -> None:
    d = evaluate("delete_namespace", {}, ctx())
    assert {"runbook_allowlisted", "params_valid"} <= failed(d)


def test_extra_parameters_blocked() -> None:
    params = {"deployment": "checkout", "command": "kubectl delete ns sentinelops-demo"}
    d = evaluate("rollback_deployment", params, ctx())
    assert "params_valid" in failed(d)


def test_scale_bounds() -> None:
    ok = {"deployment": "checkout", "replicas": 3}
    d = evaluate(
        "scale_deployment", ok, ctx(approval=approval(runbook_id="scale_deployment", params=ok))
    )
    assert d.allowed, d.failed
    too_many = {"deployment": "checkout", "replicas": 8}
    d2 = evaluate(
        "scale_deployment",
        too_many,
        ctx(approval=approval(runbook_id="scale_deployment", params=too_many)),
    )
    assert "scale_within_bounds" in failed(d2)


def test_flag_must_be_allowlisted_and_currently_on() -> None:
    p = {"deployment": "checkout", "flag": "FEATURE_NEW_STOCK_INDEX"}
    a = approval(runbook_id="disable_feature_flag", params=p)
    assert evaluate("disable_feature_flag", p, ctx(approval=a)).allowed
    off = DeploymentSnapshot(
        namespace="sentinelops-demo",
        name="checkout",
        exists=True,
        revision=3,
        revisions=(3,),
        env={"FEATURE_NEW_STOCK_INDEX": "off"},
    )
    assert "flag_currently_on" in failed(
        evaluate("disable_feature_flag", p, ctx(approval=a, deployment=off))
    )
    other = {"deployment": "checkout", "flag": "FEATURE_SOMETHING_ELSE"}
    a2 = approval(runbook_id="disable_feature_flag", params=other)
    assert "flag_allowlisted" in failed(evaluate("disable_feature_flag", other, ctx(approval=a2)))


def test_rollback_needs_an_older_revision() -> None:
    only = DeploymentSnapshot(
        namespace="sentinelops-demo", name="checkout", exists=True, revision=3, revisions=(3,)
    )
    assert "rollback_target_available" in failed(
        evaluate("rollback_deployment", PARAMS, ctx(deployment=only))
    )


def test_missing_target_blocked() -> None:
    gone = DeploymentSnapshot(namespace="sentinelops-demo", name="checkout", exists=False)
    assert "target_exists" in failed(evaluate("rollback_deployment", PARAMS, ctx(deployment=gone)))


def test_all_checks_reported_even_when_blocked() -> None:
    d = evaluate("rollback_deployment", PARAMS, ctx(mode=RemediationMode.DISABLED, approval=None))
    names = {c.name for c in d.checks}
    assert {"mode_allows_writes", "approval_present", "incident_state", "cooldown_elapsed"} <= names


def test_request_hash_is_canonical() -> None:
    a = compute_request_hash(INCIDENT, "rollback_deployment", {"deployment": "checkout"}, 3)
    b = compute_request_hash(
        INCIDENT,
        "rollback_deployment",
        {"namespace": "sentinelops-demo", "deployment": "checkout"},
        3,
    )
    assert a == b, "defaults are normalized before hashing"
    assert a != compute_request_hash(INCIDENT, "rollback_deployment", {"deployment": "checkout"}, 4)


def test_catalog_has_no_generic_command_runbook() -> None:
    ids = {r["id"] for r in describe_catalog()}
    assert ids == {
        "restart_deployment",
        "rollback_deployment",
        "scale_deployment",
        "disable_feature_flag",
    }
    with pytest.raises(ValueError, match="valid"):
        parse_params("run_command", {"cmd": "rm -rf /"})
