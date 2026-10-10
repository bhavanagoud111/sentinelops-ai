"""Engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    url: str = Field(
        default="postgresql+asyncpg://sentinel:sentinel@localhost:15432/sentinelops",
        validation_alias="DATABASE_URL",
    )
    pool_size: int = Field(default=5, ge=1, le=50, validation_alias="DATABASE_POOL_SIZE")
    statement_timeout_ms: int = Field(
        default=10_000, ge=100, validation_alias="DATABASE_STATEMENT_TIMEOUT_MS"
    )


def create_engine(settings: DatabaseSettings | None = None) -> AsyncEngine:
    cfg = settings or DatabaseSettings()
    return create_async_engine(
        cfg.url,
        pool_size=cfg.pool_size,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": str(cfg.statement_timeout_ms)}},
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One unit of work: commits on success, rolls back on any exception."""
    async with factory() as session, session.begin():
        yield session
