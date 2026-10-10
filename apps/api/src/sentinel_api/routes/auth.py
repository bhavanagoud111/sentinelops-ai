"""Auth endpoints. ``/auth/dev-token`` exists only when dev login is enabled."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from sentinel_api.auth import DEV_USERS, Principal, Reader, TokenService
from sentinel_api.schemas import TokenRequest, TokenResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _response(token: str, principal: Principal) -> TokenResponse:
    return TokenResponse(
        access_token=token,
        subject=principal.subject,
        name=principal.name,
        roles=sorted(r.value for r in principal.roles),
        permissions=sorted(p.value for p in principal.permissions),
    )


@router.post("/dev-token", response_model=TokenResponse)
def dev_token(body: TokenRequest, request: Request) -> TokenResponse:
    if not request.app.state.dev_login:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    user = DEV_USERS.get(body.username)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown dev user")
    name, roles = user
    tokens: TokenService = request.app.state.tokens
    token = tokens.issue(body.username, name, roles)
    return _response(token, tokens.verify(token))


@router.get("/dev-users")
def dev_users(request: Request) -> list[dict[str, object]]:
    if not request.app.state.dev_login:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return [
        {"username": u, "name": n, "roles": [r.value for r in roles]}
        for u, (n, roles) in DEV_USERS.items()
    ]


@router.get("/me", response_model=TokenResponse)
def me(principal: Reader, request: Request) -> TokenResponse:
    token = (request.headers.get("authorization") or "").partition(" ")[2]
    return _response(token, principal)
