"""Typed, read-only investigation tools.

``Toolbox.collect`` runs the standard evidence plan item for one (kind, service).
``Toolbox.query_*`` are the generic tools an LLM may call for follow-up checks —
every call passes through :mod:`guards` before reaching a backend.

Implementations:
* :class:`LiveToolbox`     — Prometheus / Loki / Tempo / Kubernetes (read-only)
* :class:`FixtureToolbox`  — canned evidence for deterministic evaluation and CI
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sentinel_policy import TARGET_NAMESPACE
from sentinel_telemetry import LokiClient, PrometheusClient, TelemetryError, TempoClient

from sentinel_investigator import guards, signals
from sentinel_investigator.models import Evidence, EvidenceKind


@dataclass(frozen=True)
class ToolContext:
    incident_id: uuid.UUID
    scope: frozenset[str]
    start: datetime
    end: datetime
    namespace: str = TARGET_NAMESPACE


class KubernetesReader(Protocol):
    """Read-only Kubernetes view (implemented in Phase 4 with a scoped ServiceAccount)."""

    async def rollouts(self, namespace: str, deployment: str) -> dict[str, Any]: ...

    async def pod_status(self, namespace: str, deployment: str) -> dict[str, Any]: ...


class Toolbox(Protocol):
    async def collect(self, kind: EvidenceKind, service: str, ctx: ToolContext) -> Evidence: ...

    async def query_metrics(
        self, promql: str, step_s: int, ctx: ToolContext, service: str
    ) -> Evidence: ...

    async def query_logs(
        self, logql: str, limit: int, ctx: ToolContext, service: str
    ) -> Evidence: ...

    async def search_traces(
        self, traceql: str, limit: int, ctx: ToolContext, service: str
    ) -> Evidence: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _ev(
    ctx: ToolContext,
    kind: EvidenceKind,
    service: str,
    source: str,
    tool: str,
    query: str,
    summary: str,
    sig: dict[str, Any],
    error: str | None = None,
) -> Evidence:
    anomalous = bool(sig.pop("anomalous", False)) and error is None
    return Evidence.make(
        incident_id=ctx.incident_id,
        kind=kind,
        service=service,
        source=source,
        tool=tool,
        query=query,
        start=ctx.start,
        end=ctx.end,
        retrieved_at=_now(),
        summary=summary,
        anomalous=anomalous,
        signals=sig,
        error=error,
    )


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def _ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 1000:.0f} ms"


class LiveToolbox:
    def __init__(
        self,
        prometheus: PrometheusClient,
        loki: LokiClient,
        tempo: TempoClient,
        kubernetes: KubernetesReader | None = None,
    ) -> None:
        self.prom = prometheus
        self.loki = loki
        self.tempo = tempo
        self.k8s = kubernetes

    # ------------------------------------------------------------ generic tools
    async def query_metrics(
        self, promql: str, step_s: int, ctx: ToolContext, service: str
    ) -> Evidence:
        guards.check_range(ctx.start, ctx.end)
        guards.check_promql(promql, ctx.scope, step_s)
        result = await self.prom.query_range(promql, ctx.start, ctx.end, step_s)
        prof = signals.series_profile(result, abs_threshold=0.0)
        prof["series"] = len(result)
        return _ev(
            ctx,
            EvidenceKind.ERROR_RATIO if "5xx" in promql else EvidenceKind.REQUEST_RATE,
            service,
            "prometheus",
            "query_metrics",
            promql,
            f"{service}: custom PromQL peak={prof.get('peak')} baseline={prof.get('baseline')}",
            prof,
        )

    async def query_logs(self, logql: str, limit: int, ctx: ToolContext, service: str) -> Evidence:
        guards.check_range(ctx.start, ctx.end)
        guards.check_logql(logql, ctx.scope, limit)
        streams = await self.loki.query_range(logql, ctx.start, ctx.end, limit)
        prof = signals.log_profile(streams)
        top = prof["top_messages"][0]["example"] if prof["top_messages"] else "none"
        return _ev(
            ctx,
            EvidenceKind.ERROR_LOGS,
            service,
            "loki",
            "query_logs",
            logql,
            f"{service}: {prof['total']} matching log lines; top: {top}",
            prof,
        )

    async def search_traces(
        self, traceql: str, limit: int, ctx: ToolContext, service: str
    ) -> Evidence:
        guards.check_range(ctx.start, ctx.end)
        guards.check_traceql(traceql, ctx.scope, limit)
        traces = await self.tempo.search(traceql, ctx.start, ctx.end, limit)
        prof = signals.trace_profile(traces)
        return _ev(
            ctx,
            EvidenceKind.ERROR_TRACES,
            service,
            "tempo",
            "search_traces",
            traceql,
            f"{service}: {prof['count']} matching traces",
            prof,
        )

    # ------------------------------------------------------------ evidence plan
    async def collect(self, kind: EvidenceKind, service: str, ctx: ToolContext) -> Evidence:
        try:
            return await self._collect(kind, service, ctx)
        except (TelemetryError, guards.GuardError) as exc:
            return _ev(
                ctx,
                kind,
                service,
                _SOURCE[kind],
                f"collect:{kind.value}",
                "",
                f"{service}: {kind.value} unavailable ({exc})",
                {},
                error=str(exc)[:300],
            )

    async def _collect(self, kind: EvidenceKind, service: str, ctx: ToolContext) -> Evidence:
        sel = f'service_name="{service}"'
        if kind is EvidenceKind.ERROR_RATIO:
            q = f"service:http_errors_5xx:ratio1m{{{sel}}}"
            prof = signals.series_profile(
                await self.prom.query_range(q, ctx.start, ctx.end, 15), abs_threshold=0.05
            )
            return _ev(
                ctx,
                kind,
                service,
                "prometheus",
                "query_metrics",
                q,
                f"{service} 5xx ratio peak {_pct(prof.get('peak'))} "
                f"(baseline {_pct(prof.get('baseline'))}, onset {prof.get('onset_at')})",
                prof,
            )
        if kind is EvidenceKind.LATENCY_P95:
            q = f"service:http_latency_seconds:p95_1m{{{sel}}}"
            prof = signals.series_profile(
                await self.prom.query_range(q, ctx.start, ctx.end, 15), abs_threshold=0.5
            )
            return _ev(
                ctx,
                kind,
                service,
                "prometheus",
                "query_metrics",
                q,
                f"{service} p95 latency peak {_ms(prof.get('peak'))} "
                f"(baseline {_ms(prof.get('baseline'))}, onset {prof.get('onset_at')})",
                prof,
            )
        if kind is EvidenceKind.REQUEST_RATE:
            q = f"service:http_requests:rate1m{{{sel}}}"
            prof = signals.drop_profile(await self.prom.query_range(q, ctx.start, ctx.end, 15))
            return _ev(
                ctx,
                kind,
                service,
                "prometheus",
                "query_metrics",
                q,
                f"{service} request rate latest {prof.get('latest')} req/s "
                f"(baseline {prof.get('baseline')})",
                prof,
            )
        if kind is EvidenceKind.DEPENDENCY_CALLS:
            # Services count failed outbound calls explicitly (timeouts are not always
            # visible in client duration metrics): <svc>_dependency_errors_total{dependency}.
            q_err = (
                f'sum by (dependency) (rate({{__name__=~".+_dependency_errors_total",{sel}}}[1m]))'
            )
            q_lat = (
                "histogram_quantile(0.95, sum by (server_address, le) "
                f"(rate(http_client_request_duration_seconds_bucket{{{sel}}}[1m])))"
            )
            errors = signals.per_label_profile(
                await self.prom.query_range(q_err, ctx.start, ctx.end, 15),
                "dependency",
                abs_threshold=0.1,
            )
            latency = signals.per_label_profile(
                await self.prom.query_range(q_lat, ctx.start, ctx.end, 15),
                "server_address",
                abs_threshold=0.5,
            )
            deps = sorted(set(errors) | set(latency))
            sig: dict[str, Any] = {
                "dependencies": {
                    d: {
                        "error_rate_peak": errors.get(d, {}).get("peak", 0.0),
                        "p95_peak_s": latency.get(d, {}).get("peak"),
                        "p95_baseline_s": latency.get(d, {}).get("baseline"),
                        "errors_anomalous": errors.get(d, {}).get("anomalous", False),
                        "latency_anomalous": latency.get(d, {}).get("anomalous", False),
                    }
                    for d in deps
                }
            }
            bad = [
                d
                for d, v in sig["dependencies"].items()
                if v["errors_anomalous"] or v["latency_anomalous"]
            ]
            sig["anomalous"] = bool(bad)
            sig["degraded_dependencies"] = bad
            sig["active"] = any(
                errors.get(d, {}).get("active") or latency.get(d, {}).get("active") for d in bad
            )
            return _ev(
                ctx,
                kind,
                service,
                "prometheus",
                "query_metrics",
                f"{q_err}\n{q_lat}",
                f"{service} outbound calls: degraded={bad or 'none'}",
                sig,
            )
        if kind is EvidenceKind.PROCESS_MEMORY:
            # max, not sum: one leaking process is the signal, and replaced pods' series
            # linger ~5 min after exit, so summing them inflates the baseline.
            q = f"max(process_resident_memory_bytes{{{sel}}})"
            prof = signals.series_profile(
                await self.prom.query_range(q, ctx.start, ctx.end, 15),
                abs_threshold=200 * 1024 * 1024,
                rel_factor=1.5,
            )
            return _ev(
                ctx,
                kind,
                service,
                "prometheus",
                "query_metrics",
                q,
                f"{service} RSS peak {round((prof.get('peak') or 0) / 2**20)} MiB "
                f"(baseline {round((prof.get('baseline') or 0) / 2**20)} MiB)",
                prof,
            )
        if kind is EvidenceKind.ERROR_LOGS:
            q = f'{{{sel}}} | severity_text=~"(?i)error|critical"'
            prof = signals.log_profile(await self.loki.query_range(q, ctx.start, ctx.end, 200))
            top = prof["top_messages"][0]["example"] if prof["top_messages"] else "none"
            return _ev(
                ctx,
                kind,
                service,
                "loki",
                "query_logs",
                q,
                f"{service}: {prof['total']} error logs; most common: {top}",
                prof,
            )
        if kind is EvidenceKind.ERROR_TRACES:
            q = f'{{resource.service.name="{service}" && status=error}}'
            prof = signals.trace_profile(await self.tempo.search(q, ctx.start, ctx.end, 20))
            return _ev(
                ctx,
                kind,
                service,
                "tempo",
                "search_traces",
                q,
                f"{service}: {prof['count']} error traces; roots {prof['root_spans'][:2]}",
                prof,
            )
        if kind is EvidenceKind.SLOW_TRACES:
            q = f'{{resource.service.name="{service}" && duration > 500ms}}'
            prof = signals.trace_profile(await self.tempo.search(q, ctx.start, ctx.end, 20))
            return _ev(
                ctx,
                kind,
                service,
                "tempo",
                "search_traces",
                q,
                f"{service}: {prof['count']} traces slower than 500 ms (max {prof['max_ms']} ms)",
                prof,
            )
        if kind in (EvidenceKind.ROLLOUTS, EvidenceKind.POD_STATUS):
            if self.k8s is None:
                raise TelemetryError("kubernetes reader not configured")
            if kind is EvidenceKind.ROLLOUTS:
                data = await self.k8s.rollouts(ctx.namespace, service)
                recent = [
                    r
                    for r in data.get("revisions", [])
                    if (at := r.get("rolled_out_at") or r.get("created_at"))
                    and at >= ctx.start.isoformat()
                ]
                data["recent_changes"] = recent
                data["anomalous"] = bool(recent)
                return _ev(
                    ctx,
                    kind,
                    service,
                    "kubernetes",
                    "get_recent_rollouts",
                    f"deployment/{service}",
                    f"{service}: revision {data.get('current_revision')}, "
                    f"{len(recent)} rollout(s) in window"
                    + (f"; latest change: {recent[-1].get('change')}" if recent else ""),
                    data,
                )
            data = await self.k8s.pod_status(ctx.namespace, service)
            return _ev(
                ctx,
                kind,
                service,
                "kubernetes",
                "list_pods",
                f"deployment/{service}",
                f"{service}: {data.get('ready', 0)}/{data.get('desired', 0)} ready, "
                f"restarts={data.get('restarts', 0)}, waiting={data.get('waiting_reasons')}",
                data,
            )
        raise guards.GuardError(f"unsupported evidence kind {kind}")


_SOURCE = {
    EvidenceKind.ERROR_RATIO: "prometheus",
    EvidenceKind.LATENCY_P95: "prometheus",
    EvidenceKind.REQUEST_RATE: "prometheus",
    EvidenceKind.DEPENDENCY_CALLS: "prometheus",
    EvidenceKind.PROCESS_MEMORY: "prometheus",
    EvidenceKind.ERROR_LOGS: "loki",
    EvidenceKind.ERROR_TRACES: "tempo",
    EvidenceKind.SLOW_TRACES: "tempo",
    EvidenceKind.ROLLOUTS: "kubernetes",
    EvidenceKind.POD_STATUS: "kubernetes",
}


@dataclass
class FixtureToolbox:
    """Serves canned observations: ``{"<kind>|<service>": {summary, anomalous, signals}}``."""

    observations: dict[str, dict[str, Any]]
    calls: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> FixtureToolbox:
        data = json.loads(path.read_text())
        return cls(observations=data["observations"])

    async def collect(self, kind: EvidenceKind, service: str, ctx: ToolContext) -> Evidence:
        key = f"{kind.value}|{service}"
        self.calls.append(key)
        obs = self.observations.get(key)
        if obs is None:
            return _ev(
                ctx,
                kind,
                service,
                _SOURCE[kind],
                f"collect:{kind.value}",
                key,
                f"{service}: no {kind.value} data",
                {"anomalous": False},
            )
        sig = dict(obs.get("signals", {}))
        sig["anomalous"] = obs.get("anomalous", False)
        return _ev(
            ctx, kind, service, _SOURCE[kind], f"collect:{kind.value}", key, obs["summary"], sig
        )

    async def _none(self, tool: str, query: str, ctx: ToolContext, service: str) -> Evidence:
        self.calls.append(f"{tool}|{query}")
        return _ev(
            ctx,
            EvidenceKind.ERROR_LOGS,
            service,
            "fixture",
            tool,
            query,
            f"{service}: no additional data in fixture",
            {"anomalous": False},
        )

    async def query_metrics(
        self, promql: str, step_s: int, ctx: ToolContext, service: str
    ) -> Evidence:
        guards.check_promql(promql, ctx.scope, step_s)
        return await self._none("query_metrics", promql, ctx, service)

    async def query_logs(self, logql: str, limit: int, ctx: ToolContext, service: str) -> Evidence:
        guards.check_logql(logql, ctx.scope, limit)
        return await self._none("query_logs", logql, ctx, service)

    async def search_traces(
        self, traceql: str, limit: int, ctx: ToolContext, service: str
    ) -> Evidence:
        guards.check_traceql(traceql, ctx.scope, limit)
        return await self._none("search_traces", traceql, ctx, service)
