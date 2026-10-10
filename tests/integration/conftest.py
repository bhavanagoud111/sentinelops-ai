"""Shared fixtures for integration tests against the local stack (`make up`)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ROOT = Path(__file__).resolve().parents[2]
API = f"http://127.0.0.1:{os.environ.get('API_PORT', '18000')}"
INGEST = f"http://127.0.0.1:{os.environ.get('INGESTION_PORT', '18001')}"
PG = f"127.0.0.1:{os.environ.get('POSTGRES_PORT', '15432')}"
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "sentinel")
KAFKA = f"127.0.0.1:{os.environ.get('KAFKA_PORT', '19092')}"
COMPOSE_FILE = ROOT / "infra" / "compose" / "docker-compose.yml"


def docker_bin() -> str:
    found = shutil.which("docker")
    if found:
        return found
    fallback = "/Applications/Docker.app/Contents/Resources/bin/docker"
    if Path(fallback).exists():
        return fallback
    pytest.skip("docker CLI not available")


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + ":/Applications/Docker.app/Contents/Resources/bin"
    return subprocess.run(
        [docker_bin(), "compose", "-f", str(COMPOSE_FILE), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def wait_for(predicate: Callable[[], Any], timeout: float = 30.0, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


@pytest.fixture(scope="session")
def stack_up() -> None:
    try:
        httpx.get(f"{API}/healthz", timeout=2).raise_for_status()
        httpx.get(f"{INGEST}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("control plane not running (make up)")


@pytest.fixture(scope="session")
def ingest_token(stack_up: None) -> str:
    return (ROOT / ".cache" / "secrets" / "ingest_token").read_text().strip()


@pytest.fixture(scope="session")
def token_for(stack_up: None) -> Callable[[str], str]:
    cache: dict[str, str] = {}

    def _get(username: str) -> str:
        if username not in cache:
            r = httpx.post(f"{API}/api/v1/auth/dev-token", json={"username": username}, timeout=5)
            r.raise_for_status()
            cache[username] = r.json()["access_token"]
        return cache[username]

    return _get


@pytest.fixture
def api(token_for: Callable[[str], str]) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=API, headers={"Authorization": f"Bearer {token_for('ada.admin')}"}, timeout=10
    ) as client:
        yield client


def alertmanager_webhook(
    alertname: str,
    *,
    service: str = "inventory",
    status: str = "firing",
    starts_at: str | None = None,
    am_fingerprint: str | None = None,
    severity: str = "warning",
) -> dict[str, Any]:
    from datetime import UTC, datetime

    starts_at = starts_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    return {
        "version": "4",
        "status": status,
        "receiver": "sentinelops-gateway",
        "groupKey": f"{{}}:{{alertname={alertname!r}}}",
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": alertname, "service": service, "severity": severity},
                "annotations": {"summary": f"test {alertname}"},
                "startsAt": starts_at,
                "endsAt": "2026-10-10T02:30:00Z"
                if status == "resolved"
                else "0001-01-01T00:00:00Z",
                "generatorURL": "",
                "fingerprint": am_fingerprint or uuid.uuid4().hex[:16],
            }
        ],
    }


# ------------------------------------------------------------------ database
@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A fresh, fully migrated database per test (dropped afterwards)."""
    import asyncpg
    from alembic import command
    from sentinel_storage.migrate import alembic_config

    name = f"sentinel_test_{uuid.uuid4().hex[:10]}"
    try:
        admin = await asyncpg.connect(
            f"postgresql://sentinel:{PG_PASSWORD}@{PG}/postgres", timeout=3
        )
    except (OSError, asyncpg.PostgresError):
        pytest.skip("postgres not reachable (make up)")
    await admin.execute(f'CREATE DATABASE "{name}"')
    url = f"postgresql+asyncpg://sentinel:{PG_PASSWORD}@{PG}/{name}"
    engine: AsyncEngine | None = None
    try:
        import asyncio

        await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
        engine = create_async_engine(url)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
