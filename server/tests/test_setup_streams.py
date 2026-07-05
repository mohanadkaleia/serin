"""Seam fills: /v1/setup + /v1/auth/accept-invite emit meta events (ENG-65 D2/D8)."""

from __future__ import annotations

from authutil import (
    accept_invite,
    create_invite,
    do_setup,
    fetch_meta_stream_id,
    fetch_stream_events,
    join_token,
)
from httpx import AsyncClient
from msgd.core.envelope import Body, Envelope
from msgd.core.hashing import hash_event, verify_hash
from msgd.db.models import Stream
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_setup_emits_seq1_workspace_created_and_seq2_user_joined(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Setup creates the meta stream and homes workspace.created(1) + user.joined(2)."""
    body = await do_setup(client)
    ws = body["workspace_id"]

    meta = await fetch_meta_stream_id(db_session, ws)
    assert meta is not None

    events = await fetch_stream_events(db_session, meta)
    assert [e.type for e in events] == ["workspace.created", "user.joined"]
    assert [e.server_sequence for e in events] == [1, 2]

    wc, uj = events
    # Both authored by the owner, using the owner's just-minted device (D2).
    assert wc.author_user_id == body["user_id"]
    assert wc.author_device_id == body["device_id"]
    assert uj.author_user_id == body["user_id"]
    assert uj.body["payload"]["user_id"] == body["user_id"]  # owner joins themselves

    # Raw-hash discipline: re-hash the verbatim stored body AND verify the model.
    for e in events:
        assert hash_event(e.body) == e.event_hash
        assert verify_hash(Envelope(body=Body(**e.body), event_hash=e.event_hash))

    # Only the workspace-meta stream exists — no channels/dms at setup.
    kinds = (
        (await db_session.execute(select(Stream.kind).where(Stream.workspace_id == ws)))
        .scalars()
        .all()
    )
    assert list(kinds) == ["workspace-meta"]

    meta_row = await db_session.get(Stream, meta)
    assert meta_row is not None and meta_row.head_seq == 2


async def test_accept_invite_emits_user_joined_for_invitee(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Accepting an invite homes a user.joined for the invitee at the next meta seq."""
    owner = await do_setup(client)
    invite = await create_invite(client, owner["token"], role="member")
    raw = join_token(invite.json()["url"])

    accepted = await accept_invite(client, raw, email="joiner@example.com")
    assert accepted.status_code == 200, accepted.text
    joiner = accepted.json()

    meta = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta is not None
    events = await fetch_stream_events(db_session, meta)

    # setup(1,2) then the invitee's join at seq 3.
    assert [e.type for e in events] == ["workspace.created", "user.joined", "user.joined"]
    invitee_join = events[-1]
    assert invitee_join.server_sequence == 3
    assert invitee_join.author_user_id == joiner["user_id"]  # the joiner authors (D2)
    assert invitee_join.author_device_id == joiner["device_id"]
    assert invitee_join.body["payload"]["user_id"] == joiner["user_id"]

    assert hash_event(invitee_join.body) == invitee_join.event_hash
    assert verify_hash(Envelope(body=Body(**invitee_join.body), event_hash=invitee_join.event_hash))
