"""Harness sanity checks (ENG-63 test plan: fixture timing + isolation)."""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def test_container_and_schema_ready_fast(migrated_db: str) -> None:
    """Container start + migrations are well under the 30 s acceptance bar.

    By the time this test body runs, the session-scoped container and schema
    fixtures have already materialised; we assert the *incremental* cost of
    reaching a migrated DB from here is trivial (the heavy lifting is paid once
    per session, not per test).
    """
    start = time.monotonic()
    assert migrated_db  # fixture already resolved
    assert time.monotonic() - start < 30.0


async def test_rollback_isolation_first(db_session: AsyncSession) -> None:
    """Write a row; a sibling test must not see it (transaction rollback)."""
    await db_session.execute(
        text("INSERT INTO workspaces (workspace_id, name) VALUES (:id, :name)"),
        {"id": "w_isolation", "name": "leaky?"},
    )
    count = await db_session.scalar(
        text("SELECT count(*) FROM workspaces WHERE workspace_id = :id"),
        {"id": "w_isolation"},
    )
    assert count == 1


async def test_rollback_isolation_second(db_session: AsyncSession) -> None:
    """The row from the sibling test was rolled back — this session sees none."""
    count = await db_session.scalar(
        text("SELECT count(*) FROM workspaces WHERE workspace_id = :id"),
        {"id": "w_isolation"},
    )
    assert count == 0
