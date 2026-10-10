"""Rate limiter, secrets loader and audit hash chain (pure parts)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sentinel_common.ratelimit import TokenBucketLimiter
from sentinel_common.secrets import MissingSecretError, read_secret
from sentinel_storage.audit import GENESIS, compute_hash, verify_rows
from sentinel_storage.models import AuditEvent


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_token_bucket_burst_then_refill() -> None:
    clock = FakeClock()
    lim = TokenBucketLimiter(rate_per_s=2, burst=3, clock=clock)
    assert [lim.allow("a") for _ in range(4)] == [True, True, True, False]
    clock.t = 0.5  # +1 token
    assert lim.allow("a")
    assert not lim.allow("a")
    assert lim.allow("b"), "buckets are per key"


def test_token_bucket_bounds_keys() -> None:
    lim = TokenBucketLimiter(rate_per_s=1, burst=1, max_keys=10)
    for i in range(100):
        lim.allow(f"k{i}")
    assert len(lim._buckets) == 10


def test_read_secret_prefers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "s"
    f.write_text("from-file-0123456789\n")
    monkeypatch.setenv("MY_SECRET", "from-env-0123456789")
    monkeypatch.setenv("MY_SECRET_FILE", str(f))
    assert read_secret("MY_SECRET") == "from-file-0123456789"


def test_read_secret_enforces_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "short")
    monkeypatch.delenv("MY_SECRET_FILE", raising=False)
    with pytest.raises(MissingSecretError):
        read_secret("MY_SECRET")


def test_read_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.delenv("NOPE_FILE", raising=False)
    assert read_secret("NOPE", required=False) is None
    with pytest.raises(MissingSecretError):
        read_secret("NOPE")


def _chain(n: int) -> list[AuditEvent]:
    rows: list[AuditEvent] = []
    prev = GENESIS
    for i in range(n):
        at = datetime(2026, 10, 9, 12, i, tzinfo=UTC)
        kw = {
            "actor": f"user{i}",
            "action": "approval.decide",
            "resource_type": "approval",
            "resource_id": str(i),
            "details": {"decision": "approved"},
            "at": at,
        }
        h = compute_hash(prev, **kw)  # type: ignore[arg-type]
        rows.append(AuditEvent(id=i + 1, prev_hash=prev, hash=h, **kw))
        prev = h
    return rows


def test_audit_chain_verifies() -> None:
    assert verify_rows(_chain(5)) is None


def test_audit_chain_detects_tampering() -> None:
    rows = _chain(5)
    rows[2].details = {"decision": "rejected"}
    assert verify_rows(rows) == 3


def test_audit_chain_detects_deletion() -> None:
    rows = _chain(5)
    del rows[1]
    assert verify_rows(rows) == 3
