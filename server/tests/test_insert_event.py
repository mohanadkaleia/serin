"""``insert_event`` — sequencing, raw-hash discipline, gapless concurrency (ENG-65 D1/D2)."""

from __future__ import annotations

import asyncio

import pytest
from authutil import fetch_stream_events, truncate_auth_tables
from msgd.core import ids
from msgd.core.hashing import hash_event
from msgd.core.payloads import build_workspace_created_body
from msgd.core.time import now_rfc3339
from msgd.db.models import Stream, Workspace
from msgd.events.insert import UnknownStreamError, insert_event
from msgd.settings import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _body(workspace_id: str, stream_id: str) -> dict[str, object]:
    """A server-trusted body dict homed in ``stream_id`` (fresh ``event_id``)."""
    return build_workspace_created_body(
        workspace_id=workspace_id,
        stream_id=stream_id,
        author_user_id=ids.new_user_id(),
        author_device_id=ids.new_device_id(),
        client_created_at=now_rfc3339(),
        name="Acme",
    )


async def _seed_stream(db: AsyncSession, *, workspace_id: str, stream_id: str) -> None:
    db.add(Workspace(workspace_id=workspace_id, name="Acme"))
    await db.flush()
    db.add(
        Stream(
            stream_id=stream_id,
            workspace_id=workspace_id,
            kind="channel",
            name="c",
            visibility="public",
        )
    )
    await db.flush()


async def test_sequence_gapless_from_one(db_session: AsyncSession) -> None:
    """Inserts into a bootstrapped stream assign ``server_sequence`` 1, 2, 3…"""
    ws, stream = ids.new_workspace_id(), ids.new_stream_id()
    await _seed_stream(db_session, workspace_id=ws, stream_id=stream)

    seqs = []
    for _ in range(3):
        env = await insert_event(db_session, stream_id=stream, body=_body(ws, stream))
        assert env.server is not None
        seqs.append(env.server.server_sequence)
    assert seqs == [1, 2, 3]

    # head_seq reflects the last assigned sequence.
    row = await db_session.get(Stream, stream)
    assert row is not None and row.head_seq == 3


async def test_stored_body_rehashes_to_stored_hash(db_session: AsyncSession) -> None:
    """Raw-hash discipline: ``hash_event(stored body JSONB) == stored event_hash``.

    Re-hash the verbatim stored dict — NOT ``verify_hash`` on a re-parsed model.
    """
    ws, stream = ids.new_workspace_id(), ids.new_stream_id()
    await _seed_stream(db_session, workspace_id=ws, stream_id=stream)
    body = _body(ws, stream)
    env = await insert_event(db_session, stream_id=stream, body=body)

    stored = (await fetch_stream_events(db_session, stream))[0]
    assert hash_event(stored.body) == stored.event_hash
    assert env.event_hash == stored.event_hash
    assert stored.body == body  # stored verbatim


async def test_unknown_stream_raises(db_session: AsyncSession) -> None:
    """Inserting into a non-existent stream row raises ``UnknownStreamError``."""
    ws = ids.new_workspace_id()
    missing = ids.new_stream_id()
    with pytest.raises(UnknownStreamError):
        await insert_event(db_session, stream_id=missing, body=_body(ws, missing))


async def test_gapless_under_concurrency(settings: Settings, migrated_db: str) -> None:
    """N concurrent inserts to one stream (real committing sessions) ⇒ sequences
    1..N with no gaps or dupes — proving the ``UPDATE … RETURNING`` row lock (D2)."""
    engine = create_async_engine(settings.database_url)
    cleanup = create_async_engine(settings.database_url)
    await truncate_auth_tables(cleanup)  # start clean

    ws, stream = ids.new_workspace_id(), ids.new_stream_id()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Seed a committed workspace + stream row.
        async with maker() as s:
            await _seed_stream(s, workspace_id=ws, stream_id=stream)
            await s.commit()

        n = 12

        async def one_insert() -> int:
            async with maker() as s:
                env = await insert_event(s, stream_id=stream, body=_body(ws, stream))
                await s.commit()
                assert env.server is not None
                return env.server.server_sequence

        results = await asyncio.gather(*(one_insert() for _ in range(n)))
        assert sorted(results) == list(range(1, n + 1))  # gapless, no dupes

        # Persisted rows agree: contiguous 1..N.
        async with maker() as s:
            stored = await fetch_stream_events(s, stream)
        assert [e.server_sequence for e in stored] == list(range(1, n + 1))
    finally:
        await truncate_auth_tables(cleanup)  # no committed-row leakage
        await engine.dispose()
        await cleanup.dispose()
