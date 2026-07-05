"""POST /v1/setup — first-run registration + advisory-lock race (ENG-64 D1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from authutil import OWNER, auth_header, do_setup
from httpx import ASGITransport, AsyncClient
from msgd.api.app import create_app
from msgd.db.engine import get_session
from msgd.settings import Settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_AUTH_TABLES = "sessions, devices, invites, users, workspaces"


async def test_first_run_creates_owner(client: AsyncClient) -> None:
    """First setup creates the workspace + owner and returns a working token."""
    body = await do_setup(client)
    assert body["role"] == "owner"
    assert body["user_id"].startswith("u_")
    assert body["workspace_id"].startswith("w_")
    assert body["device_id"].startswith("d_")
    assert body["token"]

    # The returned token authenticates immediately.
    resp = await client.get("/v1/auth/sessions", headers=auth_header(body["token"]))
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["current"] is True


async def test_second_setup_conflicts(client: AsyncClient) -> None:
    """Once a user exists, setup returns 409 problem+json (already-initialized)."""
    await do_setup(client)
    resp = await client.post("/v1/setup", json=OWNER)
    assert resp.status_code == 409
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "/problems/already-initialized"
    assert body["status"] == 409


# --- concurrency: committing fixture (the harness client cannot race) ---------


def _committing_app(settings: Settings) -> tuple[AsyncClient, AsyncEngine]:
    """An app whose ``get_session`` yields real, independently-committing sessions.

    The shared harness ``client`` routes every request through one connection, so
    it cannot exercise the advisory-lock race. This builds a throwaway app on its
    own engine (a fresh session/connection per request) so two concurrent setups
    genuinely contend on ``pg_advisory_xact_lock``.
    """
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(settings)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, engine


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_AUTH_TABLES} RESTART IDENTITY CASCADE"))


async def test_setup_race_exactly_one_winner(settings: Settings, migrated_db: str) -> None:
    """Two concurrent setups against committing sessions ⇒ exactly one 200, one 409.

    The advisory lock serializes them: the winner inserts the owner and commits;
    the loser, unblocked, sees ``count(users) > 0`` and returns 409.
    """
    cleanup_engine = create_async_engine(settings.database_url)
    await _truncate(cleanup_engine)  # start from a genuinely empty server

    c1, e1 = _committing_app(settings)
    c2, e2 = _committing_app(settings)
    try:
        async with c1, c2:
            r1, r2 = await asyncio.gather(
                c1.post("/v1/setup", json={**OWNER, "email": "a@example.com"}),
                c2.post("/v1/setup", json={**OWNER, "email": "b@example.com"}),
            )
        assert sorted([r1.status_code, r2.status_code]) == [200, 409]
        winner = r1 if r1.status_code == 200 else r2
        assert winner.json()["role"] == "owner"
    finally:
        await _truncate(cleanup_engine)  # don't leak committed rows to other tests
        await e1.dispose()
        await e2.dispose()
        await cleanup_engine.dispose()


async def test_setup_short_password_422(client: AsyncClient) -> None:
    """A sub-12-char password is rejected by the schema as 422 problem+json."""
    resp = await client.post("/v1/setup", json={**OWNER, "password": "short"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
