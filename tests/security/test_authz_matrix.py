"""Authorization matrix against the live API: every mutating endpoint x every role.

Expected codes: 401 without/invalid token, 403 for a role lacking the permission, and
anything *other* than 401/403 (typically 404/409/422 for these synthetic ids) when the
role is allowed — proving the request passed authorization.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from tests.integration.conftest import API

pytestmark = pytest.mark.integration

INC = str(uuid.uuid4())
APPR = str(uuid.uuid4())
ENDPOINTS: list[tuple[str, str, dict[str, object], set[str]]] = [
    # method path body roles-allowed
    (
        "POST",
        f"/api/v1/incidents/{INC}/investigate",
        {},
        {"oscar.operator", "sam.sre", "ada.admin"},
    ),
    (
        "POST",
        f"/api/v1/incidents/{INC}/resolve",
        {"reason": "test reason"},
        {"oscar.operator", "sam.sre", "ada.admin"},
    ),
    ("POST", f"/api/v1/incidents/{INC}/approvals", {}, {"oscar.operator", "sam.sre", "ada.admin"}),
    (
        "POST",
        f"/api/v1/approvals/{APPR}/decision",
        {"decision": "approve", "reason": "fine"},
        {"anna.approver", "sam.sre", "ada.admin"},
    ),
    (
        "POST",
        f"/api/v1/incidents/{INC}/actions/rollback_deployment/execute",
        {"approval_id": APPR},
        {"oscar.operator", "sam.sre", "ada.admin"},
    ),
    ("POST", "/api/v1/experiments/Z", {}, {"ada.admin"}),
]
USERS = ["vera.viewer", "oscar.operator", "anna.approver", "sam.sre", "ada.admin"]


@pytest.mark.parametrize(
    ("method", "path", "body", "allowed"),
    ENDPOINTS,
    ids=[e[1].split("/api/v1/")[1][:40] for e in ENDPOINTS],
)
def test_matrix(
    method: str,
    path: str,
    body: dict[str, object],
    allowed: set[str],
    token_for: Callable[[str], str],
) -> None:
    headers = {"Idempotency-Key": f"authz-{uuid.uuid4().hex}"}
    anon = httpx.request(method, f"{API}{path}", json=body, headers=headers, timeout=10)
    assert anon.status_code == 401, f"anonymous -> {anon.status_code}"
    for user in USERS:
        r = httpx.request(
            method,
            f"{API}{path}",
            json=body,
            timeout=10,
            headers={
                **headers,
                "Authorization": f"Bearer {token_for(user)}",
                "Idempotency-Key": f"authz-{uuid.uuid4().hex}",
            },
        )
        if user in allowed:
            assert r.status_code not in (401, 403), f"{user} should pass authz, got {r.status_code}"
        else:
            assert r.status_code == 403, f"{user} should be forbidden, got {r.status_code}"


@pytest.mark.parametrize("kind", ["garbage", "alg_none", "foreign_secret", "expired"])
def test_forged_tokens_rejected(kind: str) -> None:
    now = datetime.now(UTC)
    claims = {
        "sub": "mallory",
        "roles": ["admin"],
        "iss": "sentinelops",
        "aud": "sentinelops-api",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    if kind == "garbage":
        token = "not.a.jwt"
    elif kind == "alg_none":
        token = jwt.encode(claims, key=None, algorithm="none")
    elif kind == "foreign_secret":
        token = jwt.encode(claims, "x" * 40, algorithm="HS256")
    else:
        claims["exp"] = int((now - timedelta(minutes=5)).timestamp())
        token = jwt.encode(claims, "x" * 40, algorithm="HS256")
    r = httpx.get(
        f"{API}/api/v1/incidents", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert r.status_code == 401


def test_reads_require_authentication() -> None:
    for path in [
        "/api/v1/incidents",
        "/api/v1/services",
        "/api/v1/stats",
        "/api/v1/runbooks",
        "/api/v1/events/stream",
        "/api/v1/system/status",
    ]:
        assert httpx.get(f"{API}{path}", timeout=5).status_code == 401, path
