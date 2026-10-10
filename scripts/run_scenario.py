"""Drive one demo scenario end to end through the public API.

    uv run python scripts/run_scenario.py A --approve   # inject, detect, investigate, approve, fix
    uv run python scripts/run_scenario.py C             # stops at recommendation / escalation

Steps: close stale incidents for the scenario's alert (as a human would), inject the
fault, wait for the incident and investigation, then (with --approve) request approval
as the operator, approve as a *different* user, execute, and wait for verification.
Prints a timeline with timings and writes a JSON record to --out-dir (default docs/evaluation/runs/).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:18000"
PROM = "http://127.0.0.1:19090"


@dataclass(frozen=True)
class Scenario:
    key: str
    fingerprint: str
    expect_outcome: str  # proposed | escalated
    expect_runbook: str | None
    expect_cause: tuple[str, str]


SCENARIOS = {
    "A": Scenario(
        "A",
        "local:checkout:CheckoutHigh5xxRate",
        "proposed",
        "rollback_deployment",
        ("bad_config_rollout", "checkout"),
    ),
    "B": Scenario(
        "B",
        "local:inventory:InventoryHighLatencyP95",
        "proposed",
        "disable_feature_flag",
        ("feature_flag_regression", "inventory"),
    ),
    "C": Scenario(
        "C",
        "local:checkout:CheckoutHigh5xxRate",
        "escalated",
        None,
        ("dependency_failure", "payment"),
    ),
    "D": Scenario(
        "D",
        "local:inventory:DeploymentCrashLooping",
        "proposed",
        "rollback_deployment",
        ("crash_loop_rollout", "inventory"),
    ),
    "E": Scenario(
        "E",
        "local:worker:WorkerMemoryHigh",
        "proposed",
        "restart_deployment",
        ("resource_saturation", "worker"),
    ),
}
OPEN_STATES = {
    "OPEN",
    "TRIAGING",
    "INVESTIGATING",
    "AWAITING_APPROVAL",
    "REMEDIATING",
    "VERIFYING",
    "NEEDS_HUMAN",
    "REJECTED",
    "FAILED",
}


def token(user: str) -> str:
    r = httpx.post(f"{API}/api/v1/auth/dev-token", json={"username": user}, timeout=10)
    r.raise_for_status()
    return str(r.json()["access_token"])


class Api:
    def __init__(self, user: str) -> None:
        self.c = httpx.Client(
            base_url=API, headers={"Authorization": f"Bearer {token(user)}"}, timeout=15
        )

    def get(self, path: str, **params: Any) -> Any:
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None, key: str | None = None) -> Any:
        headers = {"Idempotency-Key": key} if key else {}
        r = self.c.post(path, json=body or {}, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text}")
        return r.json()


def wait(desc: str, fn: Any, timeout: float, interval: float = 3.0) -> Any:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for: {desc}")


def alert_active(alertname: str) -> bool:
    """True while Prometheus has the alert pending or firing."""
    r = httpx.get(
        f"{PROM}/api/v1/query", params={"query": f'ALERTS{{alertname="{alertname}"}}'}, timeout=10
    )
    r.raise_for_status()
    return bool(r.json()["data"]["result"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--approve", action="store_true", help="approve and execute the proposal")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "evaluation" / "runs"))
    args = ap.parse_args()
    sc = SCENARIOS[args.scenario]
    viewer, operator, approver = Api("vera.viewer"), Api("oscar.operator"), Api("anna.approver")
    marks: dict[str, float] = {}
    record: dict[str, Any] = {"scenario": sc.key, "started_at": datetime.now(UTC).isoformat()}

    def mark(name: str) -> None:
        marks[name] = time.monotonic()
        print(f"[{marks[name] - marks.get('start', marks[name]):6.1f}s] {name}")

    marks["start"] = time.monotonic()
    run_started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    # Alertmanager does not re-send an alert that never stopped firing (repeat_interval),
    # so injecting while it is still active would never open an incident. Start clean.
    alertname = sc.fingerprint.rsplit(":", 1)[-1]
    if alert_active(alertname):
        print(f"{alertname} still active: restoring baseline and waiting for it to clear")
        subprocess.run([str(ROOT / "scripts" / "inject-fault.sh"), "reset"], check=False)
        wait(f"{alertname} to clear", lambda: not alert_active(alertname), 600, 10)

    for inc in viewer.get("/api/v1/incidents", limit=200)["items"]:
        if inc["fingerprint"] == sc.fingerprint and inc["status"] in OPEN_STATES:
            try:
                operator.post(
                    f"/api/v1/incidents/{inc['id']}/resolve",
                    {"reason": f"closing stale incident before scenario {sc.key}"},
                )
                print(f"closed stale incident {inc['id']} ({inc['status']})")
            except RuntimeError as exc:
                print(f"warning: could not close {inc['id']}: {exc}")

    subprocess.run([str(ROOT / "scripts" / "inject-fault.sh"), sc.key], check=True)
    mark("fault injected")
    # Always restore the baseline, even if the run times out or fails: a fault left
    # behind would contaminate every following scenario.
    try:

        def new_incident() -> dict[str, Any] | None:
            # New, or a recently resolved one reopened by the correlator (ADR-010).
            for i in viewer.get("/api/v1/incidents", limit=200)["items"]:
                if i["fingerprint"] == sc.fingerprint and i["status"] != "RESOLVED":
                    return dict(i)
            return None

        inc = wait("incident", new_incident, timeout=420)
        mark(f"incident open ({inc['id']}, {inc['status']})")
        iid = inc["id"]

        def investigated() -> dict[str, Any] | None:
            invs = viewer.get(f"/api/v1/incidents/{iid}/investigations")
            # Only an investigation of *this* occurrence counts (a reopened incident has older ones).
            done = [i for i in invs if i["status"] != "running" and i["started_at"] >= run_started]
            return done[0] if done else None

        inv = wait("investigation", investigated, timeout=240)
        mark(f"investigation {inv['status']}")
        top = next((h for h in inv["hypotheses"] if h["verdict"] != "rejected"), None)
        record.update(
            incident_id=iid,
            investigation=inv["status"],
            top=(top["cause_category"], top["suspected_service"]) if top else None,
            proposal=inv["proposal"],
        )
        print("  summary:", inv["summary"].replace("\n", " ")[:400])
        ok_cause = (
            top is not None and (top["cause_category"], top["suspected_service"]) == sc.expect_cause
        )
        ok_outcome = inv["status"] == sc.expect_outcome
        ok_runbook = (inv["proposal"] or {}).get("runbook_id") == sc.expect_runbook
        print(
            f"  diagnosis correct: {ok_cause}  outcome as expected: {ok_outcome}  runbook: {ok_runbook}"
        )
        record.update(
            diagnosis_correct=ok_cause, outcome_expected=ok_outcome, runbook_expected=ok_runbook
        )

        final_status = viewer.get(f"/api/v1/incidents/{iid}")["incident"]["status"]
        if args.approve and inv["status"] == "proposed":
            appr = operator.post(
                f"/api/v1/incidents/{iid}/approvals", {}, key=f"req-{uuid.uuid4().hex}"
            )
            mark(f"approval requested by oscar.operator ({appr['runbook_id']} {appr['params']})")
            approver.post(
                f"/api/v1/approvals/{appr['id']}/decision",
                {"decision": "approve", "reason": f"scenario {sc.key}: evidence reviewed"},
            )
            mark("approved by anna.approver")
            ex = operator.post(
                f"/api/v1/incidents/{iid}/actions/{appr['runbook_id']}/execute",
                {"approval_id": appr["id"]},
                key=f"exec-{uuid.uuid4().hex}",
            )
            mark(f"execution requested ({ex['id']})")
            final_status = wait(
                "verification",
                lambda: (
                    (s := viewer.get(f"/api/v1/incidents/{iid}")["incident"]["status"])
                    in {"RESOLVED", "FAILED", "NEEDS_HUMAN"}
                    and s
                ),
                timeout=420,
            )
            mark(f"incident {final_status}")
            execs = viewer.get(f"/api/v1/incidents/{iid}/executions")
            record["execution"] = execs[0] if execs else None
            if execs:
                print(
                    "  command:",
                    execs[0]["command_template"],
                    "| policy:",
                    "allowed" if execs[0]["policy"].get("allowed") else "blocked",
                )

        record.update(
            final_status=final_status,
            timings_s={k: round(v - marks["start"], 1) for k, v in marks.items() if k != "start"},
            timeline=viewer.get(f"/api/v1/incidents/{iid}/timeline"),
        )
        out = Path(args.out_dir) / f"{sc.key}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=1, default=str))
        print(f"record: {out}")
    finally:
        if not args.no_reset:
            subprocess.run([str(ROOT / "scripts" / "inject-fault.sh"), "reset"], check=False)
    success = (
        ok_cause
        and ok_outcome
        and ok_runbook
        and (not args.approve or sc.expect_outcome != "proposed" or final_status == "RESOLVED")
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
