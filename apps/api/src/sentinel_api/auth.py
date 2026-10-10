"""Authentication and role-based authorization (design spec section 13).

Tokens are JWTs (HS256 locally; the verifier is isolated so an OIDC/JWKS
verifier can replace it — see ADR-011). Roles map to explicit permissions;
endpoints depend on permissions, never on role names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMIN = "admin"


class Permission(StrEnum):
    READ = "read"
    INVESTIGATE = "investigate"
    REQUEST_APPROVAL = "request_approval"
    DECIDE_APPROVAL = "decide_approval"
    EXECUTE = "execute"
    RUN_EXPERIMENTS = "run_experiments"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.OPERATOR: frozenset(
        {Permission.READ, Permission.INVESTIGATE, Permission.REQUEST_APPROVAL, Permission.EXECUTE}
    ),
    Role.APPROVER: frozenset({Permission.READ, Permission.DECIDE_APPROVAL}),
    Role.ADMIN: frozenset(Permission),
}

# Local demo identities (dev login only). Real deployments use an IdP.
DEV_USERS: dict[str, tuple[str, list[Role]]] = {
    "vera.viewer": ("Vera (viewer)", [Role.VIEWER]),
    "oscar.operator": ("Oscar (operator)", [Role.OPERATOR]),
    "anna.approver": ("Anna (approver)", [Role.APPROVER]),
    "sam.sre": ("Sam (operator + approver)", [Role.OPERATOR, Role.APPROVER]),
    "ada.admin": ("Ada (admin)", [Role.ADMIN]),
}

ISSUER = "sentinelops"
AUDIENCE = "sentinelops-api"


@dataclass(frozen=True)
class Principal:
    subject: str
    name: str
    roles: frozenset[Role]

    @property
    def permissions(self) -> frozenset[Permission]:
        perms: set[Permission] = set()
        for role in self.roles:
            perms |= ROLE_PERMISSIONS[role]
        return frozenset(perms)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


class TokenService:
    def __init__(self, secret: str, ttl: timedelta = timedelta(hours=8)) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        self._secret = secret
        self._ttl = ttl

    def issue(self, subject: str, name: str, roles: list[Role], now: datetime | None = None) -> str:
        issued = now or datetime.now(UTC)
        claims = {
            "sub": subject,
            "name": name,
            "roles": [r.value for r in roles],
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(issued.timestamp()),
            "exp": int((issued + self._ttl).timestamp()),
        }
        return jwt.encode(claims, self._secret, algorithm="HS256")

    def verify(self, token: str) -> Principal:
        claims = jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],  # pinned: never accept "none" or asymmetric confusion
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["sub", "exp", "iat", "roles"]},
        )
        roles = frozenset(Role(r) for r in claims["roles"] if r in Role._value2member_map_)
        return Principal(subject=str(claims["sub"]), name=str(claims.get("name", "")), roles=roles)


_bearer = HTTPBearer(auto_error=False)

# Browsers' EventSource cannot set headers, so the SSE stream alone accepts
# ?access_token=. Everywhere else tokens in URLs (logs, history, Referer) are refused.
QUERY_TOKEN_PATHS = frozenset({"/api/v1/events/stream"})


def _query_token(request: Request) -> str | None:
    if request.method == "GET" and request.url.path in QUERY_TOKEN_PATHS:
        return request.query_params.get("access_token")
    return None


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    token = credentials.credentials if credentials else _query_token(request)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token", {"WWW-Authenticate": "Bearer"}
        )
    tokens: TokenService = request.app.state.tokens
    try:
        return tokens.verify(token)
    except jwt.PyJWTError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid token", {"WWW-Authenticate": "Bearer"}
        ) from None


def requires(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory: ``Annotated[Principal, Depends(requires(Permission.READ))]``."""

    def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission: {permission}")
        return principal

    return dependency


Reader = Annotated[Principal, Depends(requires(Permission.READ))]
Investigator = Annotated[Principal, Depends(requires(Permission.INVESTIGATE))]
ApprovalRequester = Annotated[Principal, Depends(requires(Permission.REQUEST_APPROVAL))]
ApprovalDecider = Annotated[Principal, Depends(requires(Permission.DECIDE_APPROVAL))]
Executor = Annotated[Principal, Depends(requires(Permission.EXECUTE))]
Experimenter = Annotated[Principal, Depends(requires(Permission.RUN_EXPERIMENTS))]
