"""Deterministic rules provider — the zero-cost default and the evaluation baseline.

Encodes the playbook a careful on-call engineer would follow, using only the
bounded evidence signals. Every hypothesis cites the evidence that triggered it.
"""

from __future__ import annotations

import re
from typing import Any

from sentinel_investigator.models import (
    CauseCategory,
    CriticFinding,
    Evidence,
    EvidenceKind,
    HypothesesOutput,
    Hypothesis,
)
from sentinel_investigator.providers.base import AnalysisInput, ProviderResult
from sentinel_investigator.tools import Toolbox, ToolContext

K = EvidenceKind
_FLAG_ON = re.compile(
    r"(FEATURE_[A-Z0-9_]+)(?::\s*\S+\s*(?:->|→)\s*|=)on\b", re.I
)  # "FEATURE_X: off -> on" or "FEATURE_X=on" — never "on -> off"
_CONFIG_WORDS = re.compile(r"config|configuration|invalid .*url|settings", re.I)
_TIMEOUT_WORDS = re.compile(r"timeout|timed out|deadline", re.I)


class _Index:
    def __init__(self, evidence: list[Evidence]) -> None:
        self._by: dict[tuple[EvidenceKind, str], Evidence] = {}
        for e in evidence:
            self._by[(e.kind, e.service)] = e

    def get(self, kind: EvidenceKind, service: str) -> Evidence | None:
        return self._by.get((kind, service))

    def anomalous(self, kind: EvidenceKind, service: str) -> Evidence | None:
        e = self.get(kind, service)
        return e if e and e.anomalous else None


def _recency(e: Evidence | None) -> float:
    """1.0 if the anomaly is still active at the end of the window, else 0.6.

    An anomaly that already recovered (e.g. an earlier, unrelated incident inside the
    lookback window) is weaker evidence for the *current* incident. Evidence without an
    ``active`` signal (fixtures, k8s state) counts as active.
    """
    if e is None:
        return 1.0
    return 1.0 if e.signals.get("active", True) else 0.6


def _mib(v: object) -> str:
    return f"{float(v) / 2**20:.0f} MiB" if isinstance(v, int | float) else "n/a"


def _ids(*evs: Evidence | None) -> list[str]:
    return [e.evidence_id for e in evs if e is not None]


def _top_log_text(e: Evidence | None) -> str:
    if not e:
        return ""
    return " ".join(m.get("example", "") for m in e.signals.get("top_messages", []))


def _recent_change_text(e: Evidence | None) -> str:
    if not e:
        return ""
    return " ".join(str(c.get("change", "")) for c in e.signals.get("recent_changes", []))


class RulesProvider:
    name = "rules"

    async def hypothesize(
        self, data: AnalysisInput, toolbox: Toolbox, ctx: ToolContext, max_tool_calls: int
    ) -> ProviderResult:
        idx = _Index(data.evidence)
        hyps: list[Hypothesis] = []
        for svc in data.triage.scope:
            hyps.extend(self._for_service(svc, idx, data))
        hyps.sort(key=lambda h: h.confidence, reverse=True)
        # Deduplicate by (category, service), keep the strongest.
        seen: set[tuple[str, str]] = set()
        unique: list[Hypothesis] = []
        for h in hyps:
            key = (h.cause_category.value, h.suspected_service)
            if key not in seen:
                seen.add(key)
                unique.append(h)
        if not unique:
            unique.append(self._unknown(data, idx))
        for i, h in enumerate(unique[:5], start=1):
            h.hypothesis_id = f"h{i}"
        top = unique[0]
        summary = (
            f"Most likely cause: {top.cause_category.value} in {top.suspected_service} "
            f"(confidence {top.confidence:.2f}). {top.explanation}"
        )
        return ProviderResult(output=HypothesesOutput(hypotheses=unique[:5]), summary=summary)

    async def critique(
        self, data: AnalysisInput, output: HypothesesOutput
    ) -> tuple[list[CriticFinding], dict[str, int]]:
        return [], {}

    # ------------------------------------------------------------------ rules
    def _for_service(self, svc: str, idx: _Index, data: AnalysisInput) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        errors = idx.anomalous(K.ERROR_RATIO, svc)
        latency = idx.anomalous(K.LATENCY_P95, svc)
        logs = idx.anomalous(K.ERROR_LOGS, svc)
        rollouts = idx.anomalous(K.ROLLOUTS, svc)
        pods = idx.get(K.POD_STATUS, svc)
        memory = idx.anomalous(K.PROCESS_MEMORY, svc)
        deps = idx.anomalous(K.DEPENDENCY_CALLS, svc)
        slow = idx.anomalous(K.SLOW_TRACES, svc)
        change = _recent_change_text(rollouts)

        # R1: crash-looping pods after a rollout.
        waiting = " ".join(pods.signals.get("waiting_reasons", []) if pods else [])
        if pods and (pods.signals.get("crash_looping") or "CrashLoopBackOff" in waiting):
            conf = 0.75 + (0.15 if rollouts else 0.0)
            out.append(
                self._h(
                    CauseCategory.CRASH_LOOP_ROLLOUT,
                    svc,
                    conf,
                    f"Pods of {svc} are crash-looping (restarts="
                    f"{pods.signals.get('restarts')}, last exit: "
                    f"{pods.signals.get('last_terminated_reasons') or waiting or 'n/a'})"
                    + (f" right after a rollout ({change})." if rollouts else "."),
                    _ids(pods, rollouts, logs),
                    "rollback_deployment",
                    {"deployment": svc},
                    "Rollback restores the last revision whose pods were healthy.",
                )
            )

        # R2: error spike with configuration errors in logs, ideally after a rollout.
        if errors and logs and _CONFIG_WORDS.search(_top_log_text(logs)):
            conf = (0.6 + (0.25 if rollouts else 0.0)) * _recency(errors)
            out.append(
                self._h(
                    CauseCategory.BAD_CONFIG_ROLLOUT,
                    svc,
                    conf,
                    f"{svc} 5xx spike coincides with configuration errors in its logs"
                    + (f" and a recent rollout ({change})." if rollouts else "; no rollout seen."),
                    _ids(errors, logs, rollouts, idx.get(K.ERROR_TRACES, svc)),
                    "rollback_deployment" if rollouts else None,
                    {"deployment": svc} if rollouts else {},
                    "" if rollouts else "No rollout evidence; config may have changed out of band.",
                )
            )

        # R3: latency regression after a feature flag was switched on.
        flag_match = _FLAG_ON.search(change)
        current_env = rollouts.signals.get("current_env", {}) if rollouts else {}
        flag_on_now = flag_match is not None and (
            not current_env or str(current_env.get(flag_match.group(1), "")).lower() == "on"
        )
        if latency and flag_match is not None and flag_on_now:
            out.append(
                self._h(
                    CauseCategory.FEATURE_FLAG_REGRESSION,
                    svc,
                    0.85 * _recency(latency),
                    f"{svc} p95 latency rose after {flag_match.group(1)} was enabled ({change}).",
                    _ids(latency, rollouts, slow),
                    "disable_feature_flag",
                    {"deployment": svc, "flag": flag_match.group(1)},
                    "",
                )
            )

        # R4: this service's outbound calls to a dependency are failing/slow.
        if deps:
            for dep in deps.signals.get("degraded_dependencies", []):
                dep_svc = str(dep).split(":")[0]
                if dep_svc == svc or dep_svc not in data.service_graph.get(svc, []):
                    continue
                dep_lat = idx.anomalous(K.LATENCY_P95, dep_svc)
                dep_err = idx.anomalous(K.ERROR_RATIO, dep_svc)
                timeouts = bool(_TIMEOUT_WORDS.search(_top_log_text(logs)))
                conf = (
                    0.55 + (0.2 if (dep_lat or dep_err) else 0.0) + (0.1 if timeouts else 0.0)
                ) * _recency(deps)
                out.append(
                    self._h(
                        CauseCategory.DEPENDENCY_FAILURE,
                        dep_svc,
                        min(conf, 0.9),
                        f"{svc} calls to {dep_svc} are degraded"
                        + (
                            " and its own latency/errors are elevated"
                            if (dep_lat or dep_err)
                            else ""
                        )
                        + ("; callers log timeouts." if timeouts else ".")
                        + f" {svc} is a victim, not the cause.",
                        _ids(deps, dep_lat, dep_err, logs),
                        None,
                        {},
                        f"No allowlisted runbook fixes an unhealthy {dep_svc}; "
                        "escalate to its owner.",
                    )
                )

        # R4b: callers' error logs name a dependency with timeout/failure wording, and that
        # dependency's own latency/errors are anomalous *now*. Works even when outbound-call
        # metrics are unavailable.
        log_text = _top_log_text(logs).lower()
        for dep_svc in data.service_graph.get(svc, []):
            if dep_svc not in log_text or not _TIMEOUT_WORDS.search(log_text):
                continue
            dep_sym = idx.anomalous(K.LATENCY_P95, dep_svc) or idx.anomalous(K.ERROR_RATIO, dep_svc)
            if dep_sym is None or not dep_sym.signals.get("active", True):
                continue
            out.append(
                self._h(
                    CauseCategory.DEPENDENCY_FAILURE,
                    dep_svc,
                    0.75,
                    f"{svc} errors are dependency timeouts ('{_top_log_text(logs)[:80]}') and "
                    f"{dep_sym.summary}. {svc} is a victim, not the cause.",
                    _ids(logs, dep_sym, errors),
                    None,
                    {},
                    f"No allowlisted runbook fixes an unhealthy {dep_svc}; escalate to its owner.",
                )
            )

        # R5: memory growth (leak) on a worker/service.
        if memory:
            out.append(
                self._h(
                    CauseCategory.RESOURCE_SATURATION,
                    svc,
                    0.75 * _recency(memory),
                    f"{svc} resident memory grew from {_mib(memory.signals.get('baseline'))} to "
                    f"{_mib(memory.signals.get('peak'))} — consistent with a leak.",
                    _ids(memory, pods),
                    "restart_deployment",
                    {"deployment": svc},
                    "Restart mitigates; the leak itself needs a code fix.",
                )
            )
        return out

    def _unknown(self, data: AnalysisInput, idx: _Index) -> Hypothesis:
        svc = data.incident.service
        cited = _ids(idx.get(K.ERROR_RATIO, svc), idx.get(K.LATENCY_P95, svc))
        return self._h(
            CauseCategory.UNKNOWN,
            svc,
            0.2,
            f"Symptoms on {svc} do not match a known failure pattern.",
            cited,
            None,
            {},
            "Insufficient corroborating evidence; human investigation needed.",
        )

    @staticmethod
    def _h(
        category: CauseCategory,
        svc: str,
        confidence: float,
        explanation: str,
        evidence_ids: list[str],
        runbook: str | None,
        params: dict[str, Any],
        uncertainty: str,
    ) -> Hypothesis:
        return Hypothesis(
            hypothesis_id="h0",
            cause_category=category,
            suspected_service=svc,
            explanation=explanation,
            confidence=round(min(confidence, 0.95), 2),
            evidence_ids=evidence_ids,
            recommended_runbook_id=runbook,
            runbook_params=params,
            uncertainty=uncertainty,
            next_checks=[],
        )
