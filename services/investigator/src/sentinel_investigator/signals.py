"""Deterministic feature extraction from raw telemetry. Pure functions.

Agents never see raw dumps: they see these bounded features plus a one-line
summary, which keeps prompts small, comparable across runs, and hard to inject.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sentinel_investigator.guards import redact


def _points(result: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Flatten a Prometheus matrix (summing series per timestamp), dropping NaNs."""
    acc: dict[float, float] = {}
    for series in result:
        for ts, raw in series.get("values", []):
            v = float(raw)
            if math.isnan(v) or math.isinf(v):
                continue
            acc[float(ts)] = acc.get(float(ts), 0.0) + v
    return sorted(acc.items())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def series_profile(
    result: list[dict[str, Any]],
    *,
    abs_threshold: float,
    rel_factor: float = 3.0,
    robust_baseline: bool = True,
) -> dict[str, Any]:
    """Peak vs. baseline profile of a single (summed) series.

    baseline = median of the first quarter of the window, or the whole-window median
    if lower (``robust_baseline``): an earlier episode of the same alert at the start
    of the lookback must not become the baseline the current one is compared to.
    onset = first point that exceeds max(abs_threshold, baseline * rel_factor).
    """
    pts = _points(result)
    if not pts:
        return {"points": 0, "anomalous": False}
    values = [v for _, v in pts]
    head = sorted(values[: max(1, len(values) // 4)])
    baseline = head[len(head) // 2]
    if robust_baseline:
        baseline = min(baseline, sorted(values)[len(values) // 2])
    peak_ts, peak = max(pts, key=lambda p: p[1])
    threshold = max(abs_threshold, baseline * rel_factor)
    onset = next((ts for ts, v in pts if v > threshold), None)
    latest = values[-1]
    return {
        "points": len(pts),
        "baseline": round(baseline, 4),
        "peak": round(peak, 4),
        "peak_at": _iso(peak_ts),
        "latest": round(latest, 4),
        "onset_at": _iso(onset) if onset else None,
        "threshold": round(threshold, 4),
        # Still above threshold at the end of the window (vs. an earlier, recovered blip).
        "active": latest > threshold,
        "anomalous": peak > threshold,
    }


def drop_profile(result: list[dict[str, Any]]) -> dict[str, Any]:
    """Request-rate profile: anomalous when traffic drops by more than half."""
    # A drop is judged against the start of the window: a whole-window median would
    # already include the drop.
    prof = series_profile(result, abs_threshold=float("inf"), robust_baseline=False)
    if prof["points"] == 0:
        return prof
    base = prof["baseline"] or 0.0
    prof["anomalous"] = base > 0.2 and prof["latest"] < base * 0.5
    return prof


def per_label_profile(
    result: list[dict[str, Any]], label: str, *, abs_threshold: float
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for series in result:
        key = series.get("metric", {}).get(label, "unknown")
        out[key] = series_profile([series], abs_threshold=abs_threshold)
    return out


_NORMALIZE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,64}\b"), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"SKU-<n>"), "SKU-<n>"),
]


def normalize_message(msg: str) -> str:
    out = msg
    for pattern, repl in _NORMALIZE:
        out = pattern.sub(repl, out)
    return out


def log_profile(streams: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    trace_ids: list[str] = []
    total = 0
    for stream in streams:
        labels = stream.get("stream", {})
        exc = labels.get("exception_type") or labels.get("exception.type")
        exc_msg = labels.get("exception_message") or labels.get("exception.message")
        for _ts, line in stream.get("values", []):
            total += 1
            text = line if not exc else f"{line} [{exc}: {exc_msg or ''}]"
            key = normalize_message(redact(text, 300))
            counts[key] += 1
            samples.setdefault(key, redact(text, 300))
            tid = labels.get("trace_id")
            if tid and len(trace_ids) < 5 and tid not in trace_ids:
                trace_ids.append(tid)
    return {
        "total": total,
        "top_messages": [
            {"pattern": k, "example": samples[k], "count": c} for k, c in counts.most_common(top)
        ],
        "sample_trace_ids": trace_ids,
        "anomalous": total >= 5,
    }


def trace_profile(traces: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    spans: Counter[str] = Counter()
    services: Counter[str] = Counter()
    durations: list[float] = []
    ids: list[str] = []
    for t in traces:
        ids.append(str(t.get("traceID", "")))
        if t.get("durationMs") is not None:
            durations.append(float(t["durationMs"]))
        root = t.get("rootTraceName")
        if root:
            spans[str(root)] += 1
        if t.get("rootServiceName"):
            services[str(t["rootServiceName"])] += 1
        for ss in t.get("spanSets") or ([t["spanSet"]] if t.get("spanSet") else []):
            for span in ss.get("spans", []):
                for attr in span.get("attributes", []):
                    if attr.get("key") == "name":
                        spans[str(attr.get("value", {}).get("stringValue", ""))] += 1
    durations.sort()
    return {
        "count": len(traces),
        "root_spans": [{"name": n, "count": c} for n, c in spans.most_common(top)],
        "root_services": dict(services.most_common(top)),
        "p50_ms": durations[len(durations) // 2] if durations else None,
        "max_ms": durations[-1] if durations else None,
        "sample_trace_ids": ids[:5],
        "anomalous": len(traces) > 0,
    }
