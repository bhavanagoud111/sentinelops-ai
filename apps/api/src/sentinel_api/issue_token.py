"""Mint a sign-in token for a named person (production login without the dev picker).

Run where the API's JWT secret is available, e.g. inside the API pod:

    kubectl -n sentinelops-system exec deploy/sentinelops-api -- \\
        sentinel-issue-token --user alice@example.com --name "Alice" --role approver

Prints the token on stdout; the person pastes it into the dashboard sign-in screen.
Anyone who can exec into the API pod can mint tokens, so restrict that RBAC.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta

from sentinel_api.auth import Role, TokenService

logger = logging.getLogger("sentinel_api.issue_token")
MAX_HOURS = 24


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="sentinel-issue-token", description=__doc__.splitlines()[0])
    ap.add_argument("--user", required=True, help="stable subject id, e.g. an email address")
    ap.add_argument("--name", default="", help="display name")
    ap.add_argument(
        "--role",
        action="append",
        required=True,
        choices=[r.value for r in Role],
        help="repeatable: --role operator --role approver",
    )
    ap.add_argument("--hours", type=float, default=8.0, help=f"lifetime, max {MAX_HOURS}")
    args = ap.parse_args(argv)
    if not 0 < args.hours <= MAX_HOURS:
        ap.error(f"--hours must be in (0, {MAX_HOURS}]")
    return args


def issue(args: argparse.Namespace, secret: str) -> str:
    service = TokenService(secret, ttl=timedelta(hours=args.hours))
    roles = [Role(r) for r in dict.fromkeys(args.role)]
    return service.issue(args.user, args.name or args.user, roles)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entrypoint
    from sentinel_common.secrets import read_secret

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    args = parse_args(argv)
    token = issue(args, read_secret("JWT_SECRET", min_length=32) or "")
    # Subject and roles only; never log the token itself.
    logger.info("issued token for %s roles=%s hours=%s", args.user, args.role, args.hours)
    print(token)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
