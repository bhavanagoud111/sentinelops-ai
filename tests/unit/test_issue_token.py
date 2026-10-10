"""Production token minting CLI."""

from __future__ import annotations

import pytest
from sentinel_api.auth import Permission, Role, TokenService
from sentinel_api.issue_token import issue, parse_args

SECRET = "s" * 40


def test_issued_token_verifies_with_requested_roles() -> None:
    args = parse_args(["--user", "alice@example.com", "--name", "Alice", "--role", "approver"])
    p = TokenService(SECRET).verify(issue(args, SECRET))
    assert p.subject == "alice@example.com"
    assert p.name == "Alice"
    assert p.roles == frozenset({Role.APPROVER})
    assert p.can(Permission.DECIDE_APPROVAL)
    assert not p.can(Permission.EXECUTE)


def test_multiple_roles_and_default_name() -> None:
    args = parse_args(["--user", "sam", "--role", "operator", "--role", "approver"])
    p = TokenService(SECRET).verify(issue(args, SECRET))
    assert p.roles == frozenset({Role.OPERATOR, Role.APPROVER})
    assert p.name == "sam"


@pytest.mark.parametrize(
    "argv",
    [
        ["--user", "x", "--role", "root"],  # unknown role
        ["--user", "x", "--role", "viewer", "--hours", "48"],  # too long
        ["--user", "x", "--role", "viewer", "--hours", "0"],
        ["--role", "viewer"],  # no user
    ],
)
def test_rejects_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_token_from_other_secret_is_rejected() -> None:
    import jwt

    args = parse_args(["--user", "x", "--role", "admin"])
    with pytest.raises(jwt.InvalidSignatureError):
        TokenService(SECRET).verify(issue(args, "t" * 40))
