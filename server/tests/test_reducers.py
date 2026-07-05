"""Meta-event reducers — effects, idempotent replay, and bootstrap ordering (ENG-65 D3/D4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from authutil import fetch_stream_events
from msgd.core import ids
from msgd.core.time import now_rfc3339
from msgd.db.models import Stream, StreamMember, User, Workspace
from msgd.events.emit import emit_event
from msgd.events.reducers import apply_reducer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

StreamRow = tuple[str, str, str, str | None, str | None, int, datetime | None]
MemberRow = tuple[str, str]


def _meta_body(
    *,
    workspace_id: str,
    stream_id: str,
    author: str,
    type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": ids.new_event_id(),
        "workspace_id": workspace_id,
        "stream_id": stream_id,
        "type": type,
        "type_version": 1,
        "author_user_id": author,
        "author_device_id": ids.new_device_id(),
        "client_created_at": now_rfc3339(),
        "payload": payload,
    }


async def _seed(db: AsyncSession, *, n_users: int = 1) -> tuple[str, str, list[str]]:
    """Seed a workspace, a workspace-meta stream, and ``n_users`` users."""
    ws = ids.new_workspace_id()
    db.add(Workspace(workspace_id=ws, name="Acme"))
    await db.flush()
    users = []
    for i in range(n_users):
        uid = ids.new_user_id()
        db.add(
            User(
                user_id=uid,
                workspace_id=ws,
                email=f"u{i}@example.com",
                password_hash="x",
                display_name=f"U{i}",
                role="member",
            )
        )
        users.append(uid)
    meta = ids.new_stream_id()
    db.add(Stream(stream_id=meta, workspace_id=ws, kind="workspace-meta"))
    await db.flush()
    return ws, meta, users


async def _snapshot(db: AsyncSession) -> tuple[list[StreamRow], list[MemberRow]]:
    streams = (await db.execute(select(Stream).order_by(Stream.stream_id))).scalars().all()
    members = (
        (
            await db.execute(
                select(StreamMember).order_by(StreamMember.stream_id, StreamMember.user_id)
            )
        )
        .scalars()
        .all()
    )
    stream_rows: list[StreamRow] = [
        (s.stream_id, s.workspace_id, s.kind, s.name, s.visibility, s.head_seq, s.archived_at)
        for s in streams
    ]
    member_rows: list[MemberRow] = [(m.stream_id, m.user_id) for m in members]
    return stream_rows, member_rows


async def test_channel_created_reducer(db_session: AsyncSession) -> None:
    """Creates the channel's own stream (head_seq=0) + subscribes the creator."""
    ws, meta, [u1] = await _seed(db_session)
    cs = ids.new_stream_id()
    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=u1,
            type="channel.created",
            payload={"channel_stream_id": cs, "name": "general", "visibility": "public"},
        ),
    )
    row = await db_session.get(Stream, cs)
    assert row is not None
    assert (row.kind, row.name, row.visibility, row.head_seq) == ("channel", "general", "public", 0)
    member = await db_session.get(StreamMember, (cs, u1))
    assert member is not None


async def test_dm_created_reducer(db_session: AsyncSession) -> None:
    """Creates the DM stream + one member row per participant."""
    ws, _meta, users = await _seed(db_session, n_users=2)
    dm = ids.new_stream_id()
    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=dm,
            author=users[0],
            type="dm.created",
            payload={"dm_stream_id": dm, "member_user_ids": users},
        ),
    )
    row = await db_session.get(Stream, dm)
    assert row is not None
    assert (row.kind, row.visibility, row.head_seq) == ("dm", None, 0)
    for uid in users:
        assert await db_session.get(StreamMember, (dm, uid)) is not None


async def test_member_removed_reducer(db_session: AsyncSession) -> None:
    """member_removed deletes the row; a replay of the removal is a no-op."""
    ws, meta, users = await _seed(db_session, n_users=2)
    cs = ids.new_stream_id()
    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="channel.created",
            payload={"channel_stream_id": cs, "name": "g", "visibility": "private"},
        ),
    )
    add = _meta_body(
        workspace_id=ws,
        stream_id=cs,
        author=users[0],
        type="channel.member_added",
        payload={"channel_stream_id": cs, "user_id": users[1]},
    )
    await apply_reducer(db_session, add)
    assert await db_session.get(StreamMember, (cs, users[1])) is not None

    remove = _meta_body(
        workspace_id=ws,
        stream_id=cs,
        author=users[0],
        type="channel.member_removed",
        payload={"channel_stream_id": cs, "user_id": users[1]},
    )
    await apply_reducer(db_session, remove)
    assert await db_session.get(StreamMember, (cs, users[1])) is None
    # Replay of the removal is a harmless no-op.
    await apply_reducer(db_session, remove)
    assert await db_session.get(StreamMember, (cs, users[1])) is None


async def test_reducers_idempotent_under_replay(db_session: AsyncSession) -> None:
    """Apply a full meta sequence, snapshot, re-apply the same bodies → byte-identical
    ``streams``/``stream_members`` state (ENG-66 rebuild replay safety, D3)."""
    ws, meta, users = await _seed(db_session, n_users=2)
    cs = ids.new_stream_id()
    dm = ids.new_stream_id()
    bodies = [
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="workspace.created",
            payload={"name": "Acme"},
        ),
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="user.joined",
            payload={"user_id": users[0]},
        ),
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="channel.created",
            payload={"channel_stream_id": cs, "name": "general", "visibility": "public"},
        ),
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="channel.member_added",
            payload={"channel_stream_id": cs, "user_id": users[1]},
        ),
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="channel.renamed",
            payload={"channel_stream_id": cs, "name": "renamed"},
        ),
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=users[0],
            type="channel.archived",
            payload={"channel_stream_id": cs},
        ),
        _meta_body(
            workspace_id=ws,
            stream_id=dm,
            author=users[0],
            type="dm.created",
            payload={"dm_stream_id": dm, "member_user_ids": users},
        ),
    ]
    for body in bodies:
        await apply_reducer(db_session, body)
    await db_session.flush()
    first = await _snapshot(db_session)

    for body in bodies:
        await apply_reducer(db_session, body)
    await db_session.flush()
    second = await _snapshot(db_session)

    assert first == second


async def test_private_channel_bootstrap_ordering(db_session: AsyncSession) -> None:
    """emit_event runs reducer-before-insert (D4): a private ``channel.created`` is
    self-describing at seq 1 in its OWN stream; a public one lands in workspace-meta
    while the channel's own stream sits at ``head_seq=0``."""
    ws, meta, [u1] = await _seed(db_session)

    # Private: home == the channel's own stream. Reducer creates the row, then the
    # insert sequences the genesis event as seq 1 IN THAT STREAM.
    priv = ids.new_stream_id()
    priv_body = _meta_body(
        workspace_id=ws,
        stream_id=priv,
        author=u1,
        type="channel.created",
        payload={"channel_stream_id": priv, "name": "secret", "visibility": "private"},
    )
    env = await emit_event(db_session, home_stream_id=priv, body=priv_body)
    assert env.server is not None and env.server.server_sequence == 1
    priv_events = await fetch_stream_events(db_session, priv)
    assert [e.type for e in priv_events] == ["channel.created"]
    priv_row = await db_session.get(Stream, priv)
    assert priv_row is not None and priv_row.head_seq == 1

    # Public: home == workspace-meta; the channel's own stream is created at
    # head_seq=0 and the genesis event is appended to the meta sequence.
    pub = ids.new_stream_id()
    pub_body = _meta_body(
        workspace_id=ws,
        stream_id=meta,
        author=u1,
        type="channel.created",
        payload={"channel_stream_id": pub, "name": "general", "visibility": "public"},
    )
    pub_env = await emit_event(db_session, home_stream_id=meta, body=pub_body)
    assert pub_env.server is not None
    pub_row = await db_session.get(Stream, pub)
    assert pub_row is not None and pub_row.head_seq == 0  # its own stream untouched
    meta_events = await fetch_stream_events(db_session, meta)
    assert meta_events[-1].type == "channel.created"
    assert meta_events[-1].stream_id == meta  # homed in workspace-meta


async def test_colliding_channel_created_is_total_noop(db_session: AsyncSession) -> None:
    """SECURITY (round 1): a ``channel.created`` whose ``channel_stream_id``
    collides with an EXISTING stream mutates nothing — in particular it must NOT
    insert the colliding author's ``stream_members`` row (silent cross-stream
    read grant). Covers both public and private colliding variants, and a
    collision against a non-channel (dm) victim stream."""
    ws, meta, users = await _seed(db_session, n_users=2)
    victim_owner, attacker = users

    # Victim's private channel, owned/joined by victim_owner only.
    victim = ids.new_stream_id()
    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=victim_owner,
            type="channel.created",
            payload={"channel_stream_id": victim, "name": "secret", "visibility": "private"},
        ),
    )
    # A victim dm stream too (collisions are gated regardless of victim kind).
    victim_dm = ids.new_stream_id()
    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=victim_dm,
            author=victim_owner,
            type="dm.created",
            payload={"dm_stream_id": victim_dm, "member_user_ids": [victim_owner]},
        ),
    )
    await db_session.flush()
    before = await _snapshot(db_session)

    for visibility in ("public", "private"):
        for target in (victim, victim_dm):
            await apply_reducer(
                db_session,
                _meta_body(
                    workspace_id=ws,
                    stream_id=meta,
                    author=attacker,
                    type="channel.created",
                    payload={
                        "channel_stream_id": target,
                        "name": "innocuous",
                        "visibility": visibility,
                    },
                ),
            )
    await db_session.flush()

    # Total no-op: no membership grafted, no name/visibility/kind churn.
    assert await _snapshot(db_session) == before
    assert await db_session.get(StreamMember, (victim, attacker)) is None
    assert await db_session.get(StreamMember, (victim_dm, attacker)) is None


async def test_colliding_dm_created_is_total_noop(db_session: AsyncSession) -> None:
    """SECURITY (round 1): a ``dm.created`` whose ``dm_stream_id`` collides with
    an existing stream mutates nothing — the attacker-chosen ``member_user_ids``
    are never grafted onto the victim stream."""
    ws, meta, users = await _seed(db_session, n_users=2)
    victim_owner, attacker = users

    victim = ids.new_stream_id()
    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=meta,
            author=victim_owner,
            type="channel.created",
            payload={"channel_stream_id": victim, "name": "secret", "visibility": "private"},
        ),
    )
    await db_session.flush()
    before = await _snapshot(db_session)

    await apply_reducer(
        db_session,
        _meta_body(
            workspace_id=ws,
            stream_id=victim,
            author=attacker,
            type="dm.created",
            payload={"dm_stream_id": victim, "member_user_ids": [attacker]},
        ),
    )
    await db_session.flush()

    assert await _snapshot(db_session) == before
    assert await db_session.get(StreamMember, (victim, attacker)) is None
