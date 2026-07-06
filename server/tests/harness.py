"""Server test harness (ENG-63 D-6) — fixtures and hooks, fully type-checked.

This module holds all harness logic; ``conftest.py`` is a thin re-export shim.
The split exists because mypy maps both ``server/tests/conftest.py`` and
``cli/tests/conftest.py`` to the module name ``conftest`` and aborts on the
duplicate — ``harness`` is a unique module name, so everything here stays under
the strict gate while only the trivial shim is excluded.

Architecture:

* **Session-scoped Postgres container** (``postgres:17`` to match compose §11) —
  the container cost is paid once per test session.
* **Schema applied once via the real** ``run_migrations()`` (not
  ``metadata.create_all``) — this exercises the actual Alembic path and doubles
  as the "migration from empty == §4.2" gate, proving the GENERATED tsvector +
  GIN index DDL applies for real.
* **Per-test isolation = transaction rollback** — a connection begins an outer
  transaction, the ``AsyncSession`` is bound to it with
  ``join_transaction_mode="create_savepoint"``, and the outer transaction is
  rolled back after each test. The savepoint mode means a handler's own
  ``session.commit()`` / ``rollback()`` lands on a SAVEPOINT instead of escaping
  to (and ending) the outer transaction — ENG-65's committing accept path
  depends on this. No re-migrate, no truncate. App endpoints reach the same
  session via ``app.dependency_overrides``.
* **In-process ASGI** via ``httpx.AsyncClient(ASGITransport(...))``.

Container-backed tests are auto-marked ``integration`` so a local dev without
Docker can run ``-m "not integration"``; CI (Docker present) runs unfiltered.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from httpx_ws.transport import ASGIWebSocketTransport
from msgd.api.app import create_app
from msgd.db import engine as engine_module
from msgd.db.migrate import run_migrations
from msgd.settings import Settings
from msgd.ws.hub import SessionFactory, hub
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

# Fixtures whose presence means a test needs the ephemeral Postgres container.
_CONTAINER_FIXTURES = frozenset(
    {
        "postgres_container",
        "database_url",
        "migrated_db",
        "db_connection",
        "db_session",
        "client",
        "ws_app",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark container-backed tests ``integration``.

    Any test that requests a Postgres-container fixture is tagged so a local dev
    without Docker can skip it with ``-m "not integration"``; CI (Docker present)
    runs everything unfiltered. Pure-unit server tests (hashing, jcs, ...) stay
    unmarked and always run. Detecting by fixture keeps this correct for future
    tests without each module having to remember the marker.
    """
    for item in items:
        if _CONTAINER_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


def _asyncpg_url(container: PostgresContainer) -> str:
    """Return the container DSN as an asyncpg URL."""
    # testcontainers yields a psycopg2 URL; swap the driver for asyncpg.
    url: str = container.get_connection_url()
    return url.replace("postgresql+psycopg2", "postgresql+asyncpg")


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Start one ``postgres:17`` container for the whole test session."""
    with PostgresContainer("postgres:17") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    return _asyncpg_url(postgres_container)


@pytest.fixture(scope="session")
def settings(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Build settings pointed at the container (avoids reading the real env).

    argon2 cost is forced to weak params (ENG-64 test plan): the 64 MiB
    production profile makes a suite full of logins unacceptably slow. One
    dedicated test (``test_login::test_production_argon2_defaults``) asserts the
    *production* defaults remain the pinned values.
    """
    data_dir = tmp_path_factory.mktemp("msg-data")
    return Settings(
        database_url=database_url,
        data_dir=data_dir,
        secret_key="test-secret-key",
        log_level="WARNING",
        argon2_time_cost=1,
        argon2_memory_cost_kib=8,
        argon2_parallelism=1,
    )


@pytest.fixture(scope="session")
def migrated_db(settings: Settings) -> str:
    """Apply the real Alembic migrations once against the container.

    Doubles as the migration-from-empty gate: if ``run_migrations`` cannot
    reproduce §4.2 (including the GENERATED column DDL), every test errors here.
    """
    # env.py reads MSG_DATABASE_URL as a fallback; keep it consistent too.
    os.environ["MSG_DATABASE_URL"] = settings.database_url
    run_migrations(settings.database_url)
    return settings.database_url


@pytest_asyncio.fixture
async def db_connection(migrated_db: str) -> AsyncIterator[AsyncConnection]:
    """A connection wrapping the whole test in an outer transaction."""
    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        await conn.begin()
        yield conn
        await conn.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """An ``AsyncSession`` bound to the rolled-back outer transaction.

    ``join_transaction_mode="create_savepoint"`` pins the session's own
    transaction control to SAVEPOINTs nested inside the outer connection-level
    transaction: a ``session.commit()`` in a handler (ENG-65's accept path
    commits) releases its savepoint but never commits — or ends — the outer
    transaction, so per-test rollback isolation survives handler commits.
    """
    maker = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(settings: Settings, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """In-process ASGI client whose ``get_session`` yields the bound session.

    The dependency override keeps endpoint writes inside the rolled-back
    transaction, so app-level tests share the per-test isolation.
    """
    app = create_app(settings)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[engine_module.get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# --- WebSocket fixtures (ENG-68) ---------------------------------------------
#
# httpx==0.28 has no WS support and Starlette's sync TestClient runs the app in a
# portal thread with its own loop — fatal for the asyncpg session bound to the
# test's loop (§8). ``httpx-ws``'s ``ASGIWebSocketTransport`` layers on the same
# in-process ASGI app IN THE SAME LOOP and honors ``dependency_overrides``, so WS
# auth flows through the same overridden ``get_session`` → same rolled-back
# transaction as every other harness test. That transport also serves plain HTTP
# (it subclasses ``ASGITransport``), so one client drives both the setup / batch
# HTTP calls and the sockets.
#
# CRITICAL (why ``ws_app`` is a plain fixture, not a client fixture): the WS
# transport opens an anyio task group in ``__aenter__``. pytest-asyncio drives an
# async-generator fixture's setup and teardown in *different* tasks, and anyio
# forbids exiting a cancel scope in a task other than the one that entered it — so
# a fixture that holds the transport open across its ``yield`` blows up at
# teardown. The fixture therefore yields only the configured *app*; each test
# enters ``make_ws_client(ws_app)`` in its own task (``async with … as client``).


def bound_session_factory(session: AsyncSession) -> SessionFactory:
    """A hub ``session_factory`` yielding the bound per-test ``session`` (R1).

    Per-send fanout resolution then reads the SAME rolled-back transaction as the
    upload that triggered it — the reason the factory is injectable rather than
    hard-wired to the (test-absent) global sessionmaker (§3a). The context manager
    never closes the shared session.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


def make_ws_client(app: FastAPI) -> AsyncClient:
    """Wrap ``app`` in an in-process WS-capable ASGI client (caller uses ``async with``).

    Entered inside the test coroutine so the transport's anyio task group is opened
    and closed in the same task (see the module note above).
    """
    return AsyncClient(transport=ASGIWebSocketTransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_ws_hub() -> Iterator[None]:
    """Reset the process-global hub around every test (R2).

    The hub is a module singleton (the fanout seam has no app handle), so stale
    connections would leak across tests; ``reset_for_tests`` also restores the
    production session factory. No DB dependency, so this stays a global autouse
    without pulling the container into pure-unit tests — the bound-session
    injection lives in ``ws_app`` (below), which the WS tests request.
    """
    hub.reset_for_tests()
    yield
    hub.reset_for_tests()


@pytest.fixture
def ws_app(settings: Settings, db_session: AsyncSession) -> FastAPI:
    """A WS-ready app bound to the rolled-back ``db_session`` (§8).

    Overrides ``get_session`` (WS auth + upload) AND injects the hub's per-send
    session factory (fanout resolution) onto the same ``db_session``, so the whole
    connect → upload → fanout path shares one per-test transaction. Tests wrap it in
    ``make_ws_client`` themselves (see the module note on the task-group lifecycle).
    """
    app = create_app(settings)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[engine_module.get_session] = _override_get_session
    hub.set_session_factory(bound_session_factory(db_session))
    return app
