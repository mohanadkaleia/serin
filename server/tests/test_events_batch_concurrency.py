"""True-concurrency batch upload: gapless sequences + mid-flight idempotency (ENG-66).

Uses the committing app (real, independently-committing sessions) because the
shared rollback-isolated harness serializes every request through one session
and cannot exercise the streams-row lock or a blocking unique-index race.
"""

from __future__ import annotations

import asyncio
from typing import Any

from authutil import committing_app, truncate_auth_tables
from eventsutil import message_body, post_batch, wire_item
from httpx import AsyncClient
from msgd.core import ids
from msgd.db.models import Event, Stream
from msgd.settings import Settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


async def _setup_owner(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post(
        "/v1/setup",
        json={
            "workspace_name": "Acme",
            "email": "own@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Owner",
        },
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _bootstrap_public_channel(
    client: AsyncClient, engine: AsyncEngine, owner: dict[str, Any]
) -> str:
    """Create a public channel through the endpoint (meta id read via the engine)."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        meta = await db.scalar(
            select(Stream.stream_id).where(
                Stream.workspace_id == owner["workspace_id"],
                Stream.kind == "workspace-meta",
            )
        )
    assert meta is not None
    channel_stream_id = ids.new_stream_id()
    body = {
        "event_id": ids.new_event_id(),
        "workspace_id": owner["workspace_id"],
        "stream_id": meta,
        "type": "channel.created",
        "type_version": 1,
        "author_user_id": owner["user_id"],
        "author_device_id": owner["device_id"],
        "client_created_at": "2026-07-04T12:00:00.000Z",
        "payload": {"channel_stream_id": channel_stream_id, "name": "g", "visibility": "public"},
    }
    resp = await post_batch(client, owner["token"], [wire_item(body)])
    assert resp.status_code == 200 and len(resp.json()["accepted"]) == 1, resp.text
    return channel_stream_id


async def test_gapless_sequences_under_parallel_batches(
    settings: Settings, migrated_db: str
) -> None:
    """N parallel batches to one channel → contiguous 1..N*K sequences, no gaps/dupes."""
    cleanup_engine = create_async_engine(settings.database_url)
    await truncate_auth_tables(cleanup_engine)  # start from an empty server

    client, engine = committing_app(settings)
    try:
        async with client:
            owner = await _setup_owner(client)
            channel = await _bootstrap_public_channel(client, engine, owner)

            n_batches, per_batch = 5, 4
            batches = [
                [
                    wire_item(message_body(auth=owner, stream_id=channel, text=f"b{b} m{m}"))
                    for m in range(per_batch)
                ]
                for b in range(n_batches)
            ]
            responses = await asyncio.gather(
                *(post_batch(client, owner["token"], items) for items in batches)
            )

            all_seqs: list[int] = []
            for resp in responses:
                assert resp.status_code == 200, resp.text
                payload = resp.json()
                assert payload["rejected"] == []
                seqs = [e["server_sequence"] for e in payload["accepted"]]
                # In-batch ordering guarantee: consecutive ascending in batch order.
                assert seqs == sorted(seqs)
                all_seqs.extend(seqs)

            total = n_batches * per_batch
            assert sorted(all_seqs) == list(range(1, total + 1))  # gapless, no dupes

            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                stored = (
                    (
                        await db.execute(
                            select(Event.server_sequence).where(Event.stream_id == channel)
                        )
                    )
                    .scalars()
                    .all()
                )
                head = await db.scalar(select(Stream.head_seq).where(Stream.stream_id == channel))
            assert sorted(stored) == list(range(1, total + 1))
            assert head == total
    finally:
        await truncate_auth_tables(cleanup_engine)
        await engine.dispose()
        await cleanup_engine.dispose()


async def test_concurrent_duplicate_event_id_single_row(
    settings: Settings, migrated_db: str
) -> None:
    """Two parallel uploads of one event_id → one stored row, identical responses.

    The loser's INSERT blocks on UNIQUE(workspace_id, event_id) until the winner
    commits, raises inside its savepoint, and is recovered as an idempotent
    re-accept returning the winner's original record.
    """
    cleanup_engine = create_async_engine(settings.database_url)
    await truncate_auth_tables(cleanup_engine)

    client, engine = committing_app(settings)
    try:
        async with client:
            owner = await _setup_owner(client)
            channel = await _bootstrap_public_channel(client, engine, owner)
            item = wire_item(message_body(auth=owner, stream_id=channel, text="race"))

            r1, r2 = await asyncio.gather(
                post_batch(client, owner["token"], [item]),
                post_batch(client, owner["token"], [item]),
            )
            assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
            (e1,) = r1.json()["accepted"]
            (e2,) = r2.json()["accepted"]
            assert e1 == e2  # same sequence + received_at: the original record

            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                rows = (
                    (
                        await db.execute(
                            select(Event).where(Event.event_id == item["body"]["event_id"])
                        )
                    )
                    .scalars()
                    .all()
                )
                head = await db.scalar(select(Stream.head_seq).where(Stream.stream_id == channel))
            assert len(rows) == 1
            # The loser's savepoint rollback returned its head_seq bump, so no
            # gap: head stays at the single accepted sequence.
            assert head == e1["server_sequence"] == 1
    finally:
        await truncate_auth_tables(cleanup_engine)
        await engine.dispose()
        await cleanup_engine.dispose()
