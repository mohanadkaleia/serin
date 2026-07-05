"""Migration-drift and GENERATED-column tests (ENG-63 test plan).

These are the permanent gates that lock the ORM models, the initial migration,
and TDD §4.2 together, and that prove the tsvector GENERATED DDL is real.
"""

from __future__ import annotations

import msgd.db.models  # noqa: F401  (register tables on Base.metadata)
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from msgd.db.base import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def test_migration_matches_models(migrated_db: str) -> None:
    """After ``run_migrations`` from empty, models == migration == §4.2 (no drift).

    A non-empty diff means the ORM models and the committed migration disagree —
    i.e. the migration no longer reproduces §4.2. This gate is permanent.
    """
    engine = create_async_engine(migrated_db)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn, opts={"compare_type": True}),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()

    assert diffs == [], f"schema drift between models and migration: {diffs}"


async def test_tsvector_generated_column_populates(db_session: AsyncSession) -> None:
    """Inserting a messages_proj row auto-populates the GENERATED search_tsv.

    Proves the ``GENERATED ALWAYS AS (to_tsvector('english', text)) STORED``
    DDL applied for real (not merely a nullable column).
    """
    await db_session.execute(
        text(
            "INSERT INTO messages_proj "
            "(message_id, stream_id, author_user_id, text, created_seq) "
            "VALUES (:mid, :sid, :uid, :txt, :seq)"
        ),
        {
            "mid": "m_test",
            "sid": "s_test",
            "uid": "u_test",
            "txt": "the quick brown fox jumps",
            "seq": 1,
        },
    )
    tsv = await db_session.scalar(
        text("SELECT search_tsv FROM messages_proj WHERE message_id = :mid"),
        {"mid": "m_test"},
    )
    assert tsv is not None
    # to_tsvector('english', ...) stems and drops stopwords; "quick" survives.
    assert "quick" in tsv
