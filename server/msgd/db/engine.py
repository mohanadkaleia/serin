"""Async engine, session factory, and the FastAPI session dependency.

The engine is created in the app lifespan (:func:`msgd.api.app.create_app`) and
disposed on shutdown. :func:`get_session` is the request-scoped dependency; the
test harness overrides it to bind sessions to a rolled-back outer transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """Build the async engine (``pool_pre_ping`` for resilient long-lived pools)."""
    return create_async_engine(database_url, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)


# Populated by create_app() at startup so the request dependency can reach it.
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def set_sessionmaker(sessionmaker: async_sessionmaker[AsyncSession] | None) -> None:
    """Install (or clear) the process-wide session factory."""
    global _sessionmaker
    _sessionmaker = sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped :class:`AsyncSession`."""
    if _sessionmaker is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("session factory not initialised; create_app() must run first")
    async with _sessionmaker() as session:
        yield session
