"""Ingestion gateway under abuse: authentication, size limits, malformed input, rate limits.

These paths are rejected before any database access, so the session factory is a stub
that fails loudly if touched.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sentinel_common.ratelimit import TokenBucketLimiter
from sentinel_ingestion.app import create_app

TOKEN = "t" * 32


class NoDB:
    def __call__(self) -> Any:
        raise AssertionError("database must not be touched for rejected requests")


def client(limiter: TokenBucketLimiter | None = None) -> TestClient:
    app = create_app(sessions=NoDB(), ingest_token=TOKEN, limiter=limiter)  # type: ignore[arg-type]
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/api/v1/ingest/alertmanager"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}x"},
    ],
)
def test_rejects_bad_credentials(headers: dict[str, str]) -> None:
    assert client().post(URL, json={}, headers=headers).status_code == 401


def test_short_token_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="16"):
        create_app(sessions=NoDB(), ingest_token="short")  # type: ignore[arg-type]


def test_oversized_body_rejected() -> None:
    body = b'{"x":"' + b"a" * 1_100_000 + b'"}'
    r = client().post(URL, content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'"string"',
        b"not json",
        b'{"version":"3","status":"firing","alerts":[]}',
        b'{"version":"4","status":"exploding","alerts":[]}',
    ],
)
def test_malformed_payloads_rejected(body: bytes) -> None:
    r = client().post(URL, content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_rate_limited_per_client() -> None:
    c = client(TokenBucketLimiter(rate_per_s=0.001, burst=2))
    codes = [
        c.post(URL, content=b"[]", headers={**AUTH, "Content-Type": "application/json"}).status_code
        for _ in range(4)
    ]
    assert codes[:2] == [422, 422]
    assert codes[2:] == [429, 429]
