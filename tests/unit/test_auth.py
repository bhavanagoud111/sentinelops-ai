from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sentinel_api.auth import (
    AUDIENCE,
    ISSUER,
    ROLE_PERMISSIONS,
    Permission,
    Principal,
    Role,
    TokenService,
)

SECRET = "x" * 40


def test_round_trip() -> None:
    svc = TokenService(SECRET)
    p = svc.verify(svc.issue("anna", "Anna", [Role.APPROVER]))
    assert p.subject == "anna"
    assert p.can(Permission.DECIDE_APPROVAL)
    assert not p.can(Permission.EXECUTE)


def test_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="32"):
        TokenService("short")


def test_rejects_expired() -> None:
    svc = TokenService(SECRET, ttl=timedelta(minutes=1))
    token = svc.issue("a", "A", [Role.VIEWER], now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(jwt.ExpiredSignatureError):
        svc.verify(token)


def test_rejects_wrong_secret() -> None:
    token = TokenService("y" * 40).issue("a", "A", [Role.ADMIN])
    with pytest.raises(jwt.InvalidSignatureError):
        TokenService(SECRET).verify(token)


def test_rejects_alg_none() -> None:
    claims = {
        "sub": "evil",
        "roles": ["admin"],
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": 0,
        "exp": 9999999999,
    }
    token = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        TokenService(SECRET).verify(token)


def test_unknown_roles_are_dropped() -> None:
    svc = TokenService(SECRET)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "a",
            "roles": ["superuser", "viewer"],
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        SECRET,
        algorithm="HS256",
    )
    assert svc.verify(token).roles == frozenset({Role.VIEWER})


def test_separation_of_duties_in_role_matrix() -> None:
    # An operator can request but not decide; an approver can decide but not execute.
    assert Permission.DECIDE_APPROVAL not in ROLE_PERMISSIONS[Role.OPERATOR]
    assert Permission.EXECUTE not in ROLE_PERMISSIONS[Role.APPROVER]
    assert Permission.REQUEST_APPROVAL not in ROLE_PERMISSIONS[Role.APPROVER]
    assert ROLE_PERMISSIONS[Role.VIEWER] == frozenset({Permission.READ})


def test_multi_role_union() -> None:
    p = Principal("s", "S", frozenset({Role.OPERATOR, Role.APPROVER}))
    assert p.can(Permission.EXECUTE)
    assert p.can(Permission.DECIDE_APPROVAL)
