"""Input guards for read tools and output redaction.

Tools are read-only, but queries can still be abused (scanning unrelated
services, huge time ranges, expensive regexes) and results can carry secrets
or prompt-injection text. These guards bound all three.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

MAX_QUERY_LEN = 1500
MAX_RANGE = timedelta(hours=6)
MIN_STEP_S = 5
MAX_LOG_LIMIT = 200
MAX_TRACE_LIMIT = 50
MAX_LINE_LEN = 500


class GuardError(ValueError):
    """A tool call was refused before reaching any backend."""


_SERVICE_MATCHER = re.compile(
    r"(?:service_name|service|job|resource\.service\.name)\s*(=~|!=|!~|=)\s*\"([^\"]*)\""
)
_STREAM_SELECTOR = re.compile(r"^\s*\{([^}]*)\}")


def check_range(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise GuardError("timestamps must be timezone-aware")
    if end <= start:
        raise GuardError("end must be after start")
    if end - start > MAX_RANGE:
        raise GuardError(f"time range exceeds {MAX_RANGE}")


def _check_common(query: str) -> None:
    if not query.strip():
        raise GuardError("empty query")
    if len(query) > MAX_QUERY_LEN:
        raise GuardError(f"query longer than {MAX_QUERY_LEN} characters")


def check_service_scope(query: str, allowed: frozenset[str], *, require: bool) -> None:
    """Every service matcher must be an equality on an allowed service."""
    matchers = _SERVICE_MATCHER.findall(query)
    if require and not matchers:
        raise GuardError('query must select a service (service_name="...")')
    for op, value in matchers:
        if op != "=":
            raise GuardError("service matchers must use exact equality")
        if value not in allowed:
            raise GuardError(f"service {value!r} is outside the investigation scope")


def check_promql(query: str, allowed: frozenset[str], step_s: int) -> None:
    _check_common(query)
    if step_s < MIN_STEP_S:
        raise GuardError(f"step must be >= {MIN_STEP_S}s")
    check_service_scope(query, allowed, require=True)


def check_logql(query: str, allowed: frozenset[str], limit: int) -> None:
    _check_common(query)
    if not 1 <= limit <= MAX_LOG_LIMIT:
        raise GuardError(f"limit must be 1..{MAX_LOG_LIMIT}")
    selector = _STREAM_SELECTOR.match(query)
    if not selector:
        raise GuardError("LogQL must start with a stream selector")
    check_service_scope("{" + selector.group(1) + "}", allowed, require=True)
    check_service_scope(query, allowed, require=False)


def check_traceql(query: str, allowed: frozenset[str], limit: int) -> None:
    _check_common(query)
    if not 1 <= limit <= MAX_TRACE_LIMIT:
        raise GuardError(f"limit must be 1..{MAX_TRACE_LIMIT}")
    check_service_scope(query, allowed, require=True)


# ------------------------------------------------------------------ redaction
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)\b(bearer|token|apikey|api_key|secret|password|passwd|pwd)\b(\s*[:=]\s*|\s+)\S+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_API_KEY]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
        "[REDACTED_JWT]",
    ),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_NUMBER]"),
]


def redact(text: str, max_len: int = MAX_LINE_LEN) -> str:
    out = text
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out[:max_len]
