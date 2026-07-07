"""Reactions server side (ENG-97): validation referential + reactions_proj apply.

Covers the three ticket surfaces that are NOT already exercised by the simulation
suite's reaction invariants:

* **Validation referential (§3.2 / ENG-97).** A reaction is writable iff the
  author can write (== read) the target message's stream, AND the target message
  exists in that same stream. Absence and a cross-stream reference collapse to an
  identical non-disclosing ``unknown_message``. Duplicate-add and absent-remove
  are VALID events (idempotency is a projection concern, not a reject).
* **``reactions_proj`` apply.** Idempotent set-add / set-remove, byte-exact
  opaque-emoji handling (a base emoji and its skin-tone-modified form are distinct
  rows; identical bytes dedup), counts as a pure function of the log, and
  ``rebuild ≡ incremental`` for the reaction set.
* **End-to-end** through ``POST /v1/events/batch``.
"""

from __future__ import annotations

from typing import Any

from authutil import do_setup
from eventsutil import (
    bootstrap_channel,
    message_body,
    post_batch,
    reaction_body,
    wire_item,
)
from httpx import AsyncClient
from msgd.core import ids
from msgd.core.payloads import build_message_created_body
from msgd.core.time import now_rfc3339
from msgd.db.models import ReactionProj, Stream, Workspace
from msgd.events.insert import insert_event
from msgd.events.validate import Accepted, validate_event
from msgd.projections.dump import dump_reactions_proj
from msgd.projections.rebuild import rebuild_projections
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the seeded validation world (workspace + streams + role contexts).
from test_events_validate import _expect_rejected, _seed, _World

_THUMB = "\U0001f44d"
_THUMB_TONE = "\U0001f44d\U0001f3fd"  # base + skin-tone modifier — DISTINCT bytes
_CTRL = "\u0001"  # C1 control char - opaque, non-NUL, storable


async def _insert_message(db: AsyncSession, w: _World, stream_id: str) -> str:
    """Insert a real ``message.created`` (populating ``messages_proj``); return its id."""
    body = message_body(auth=w.auth(w.member), stream_id=stream_id)
    await insert_event(db, stream_id=stream_id, body=body)
    message_id: str = body["payload"]["message_id"]
    return message_id


# --- validation referential ---------------------------------------------------


async def test_reaction_to_existing_message_accepted(db_session: AsyncSession) -> None:
    """A reaction to a message that exists in the homed stream is Accepted."""
    w = await _seed(db_session)
    mid = await _insert_message(db_session, w, w.priv)  # member is in priv
    react = reaction_body(auth=w.auth(w.member), stream_id=w.priv, message_id=mid, emoji=_THUMB)
    out = await validate_event(db_session, ctx=w.member, item=wire_item(react))
    assert isinstance(out, Accepted), out


async def test_reaction_unknown_message_rejected(db_session: AsyncSession) -> None:
    """A reaction to a message that never existed → unknown_message."""
    w = await _seed(db_session)
    react = reaction_body(
        auth=w.auth(w.member), stream_id=w.priv, message_id=ids.new_message_id(), emoji=_THUMB
    )
    _expect_rejected(
        await validate_event(db_session, ctx=w.member, item=wire_item(react)), "unknown_message"
    )


async def test_reaction_cross_stream_is_non_disclosing(db_session: AsyncSession) -> None:
    """A message in another (readable) stream and an absent message collapse to
    the IDENTICAL unknown_message — no cross-stream existence oracle (D13)."""
    w = await _seed(db_session)
    mid = await _insert_message(db_session, w, w.priv)  # message lives in priv
    # Both reactions are homed in pub (which member can read/write), but reference
    # a message NOT in pub: one that lives in priv, one that never existed.
    cross = reaction_body(auth=w.auth(w.member), stream_id=w.pub, message_id=mid, emoji=_THUMB)
    absent = reaction_body(
        auth=w.auth(w.member), stream_id=w.pub, message_id=ids.new_message_id(), emoji=_THUMB
    )
    out_cross = _expect_rejected(
        await validate_event(db_session, ctx=w.member, item=wire_item(cross)), "unknown_message"
    )
    out_absent = _expect_rejected(
        await validate_event(db_session, ctx=w.member, item=wire_item(absent)), "unknown_message"
    )
    assert out_cross.detail == out_absent.detail  # existence not disclosed


async def test_reaction_to_unwritable_stream_denied(db_session: AsyncSession) -> None:
    """Reacting in a stream the author cannot write → permission_denied (before the
    referential check even runs — the message existing in priv is never disclosed)."""
    w = await _seed(db_session)
    mid = await _insert_message(db_session, w, w.priv)
    # owner is NOT a member of priv → cannot write it.
    react = reaction_body(auth=w.auth(w.owner), stream_id=w.priv, message_id=mid, emoji=_THUMB)
    _expect_rejected(
        await validate_event(db_session, ctx=w.owner, item=wire_item(react)), "permission_denied"
    )


async def test_duplicate_add_and_absent_remove_are_valid(db_session: AsyncSession) -> None:
    """Duplicate reaction.added (same key, new event_id) and reaction.removed of an
    absent reaction are VALID — idempotency is a projection concern, not a reject."""
    w = await _seed(db_session)
    mid = await _insert_message(db_session, w, w.priv)
    auth = w.auth(w.member)
    add1 = reaction_body(auth=auth, stream_id=w.priv, message_id=mid, emoji=_THUMB)
    add2 = reaction_body(auth=auth, stream_id=w.priv, message_id=mid, emoji=_THUMB)  # dup key
    rem_absent = reaction_body(
        auth=auth, stream_id=w.priv, message_id=mid, emoji="\U0001f389", removed=True
    )
    for item in (add1, add2, rem_absent):
        out = await validate_event(db_session, ctx=w.member, item=wire_item(item))
        assert isinstance(out, Accepted), out


async def test_unknown_version_reaction_referential_still_applies(db_session: AsyncSession) -> None:
    """A D9 unknown-version reaction still gets the referential check (payload model
    is skipped, so the check is version-agnostic): garbage message → unknown_message,
    an existing message → Accepted (stored opaquely, projected as a no-op)."""
    w = await _seed(db_session)
    mid = await _insert_message(db_session, w, w.priv)
    good = reaction_body(
        auth=w.auth(w.member), stream_id=w.priv, message_id=mid, emoji=_THUMB, type_version=2
    )
    assert isinstance(
        await validate_event(db_session, ctx=w.member, item=wire_item(good)), Accepted
    )
    bad = reaction_body(
        auth=w.auth(w.member),
        stream_id=w.priv,
        message_id=ids.new_message_id(),
        emoji=_THUMB,
        type_version=2,
    )
    _expect_rejected(
        await validate_event(db_session, ctx=w.member, item=wire_item(bad)), "unknown_message"
    )


# --- reactions_proj apply: idempotency, opaque bytes, rebuild ≡ incremental ----


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


def _reaction(*, ws: str, stream: str, user: str, mid: str, emoji: str, removed: bool) -> Any:
    return reaction_body(
        auth={"workspace_id": ws, "user_id": user, "device_id": ids.new_device_id()},
        stream_id=stream,
        message_id=mid,
        emoji=emoji,
        removed=removed,
    )


async def test_reaction_projection_idempotent_and_byte_exact(db_session: AsyncSession) -> None:
    """Apply exercises: dup-add no-op, absent-remove no-op, byte-exact opaque emoji
    (👍 vs 👍🏽 are DISTINCT rows), remove, and rebuild ≡ incremental."""
    ws, stream, user = ids.new_workspace_id(), ids.new_stream_id(), ids.new_user_id()
    await _seed_stream(db_session, workspace_id=ws, stream_id=stream)
    mid = ids.new_message_id()
    author = build_message_created_body(
        workspace_id=ws,
        stream_id=stream,
        author_user_id=user,
        author_device_id=ids.new_device_id(),
        client_created_at=now_rfc3339(),
        text="hi",
        message_id=mid,
    ).model_dump(mode="json")
    await insert_event(db_session, stream_id=stream, body=author)

    def add(emoji: str) -> Any:
        return _reaction(ws=ws, stream=stream, user=user, mid=mid, emoji=emoji, removed=False)

    def remove(emoji: str) -> Any:
        return _reaction(ws=ws, stream=stream, user=user, mid=mid, emoji=emoji, removed=True)

    # Two identical-key adds (distinct event_ids) → ONE membership row (dedup).
    await insert_event(db_session, stream_id=stream, body=add(_THUMB))
    await insert_event(db_session, stream_id=stream, body=add(_THUMB))
    # Skin-tone form is a DISTINCT byte sequence → its own row (must not merge).
    await insert_event(db_session, stream_id=stream, body=add(_THUMB_TONE))
    # A control char is opaque bytes, stored faithfully.
    await insert_event(db_session, stream_id=stream, body=add(_CTRL))
    # Remove of a never-added emoji → no-op.
    await insert_event(db_session, stream_id=stream, body=remove("\U0001f389"))

    emojis = set(
        (await db_session.execute(select(ReactionProj.emoji).where(ReactionProj.message_id == mid)))
        .scalars()
        .all()
    )
    assert emojis == {_THUMB, _THUMB_TONE, _CTRL}
    # Count for (mid, 👍) is exactly 1 — a pure function of the log (dup collapsed).
    thumb_count = await db_session.scalar(
        select(func.count()).where(ReactionProj.message_id == mid, ReactionProj.emoji == _THUMB)
    )
    assert thumb_count == 1

    # rebuild ≡ incremental for the reaction set.
    before = await dump_reactions_proj(db_session)
    await rebuild_projections(db_session)
    assert await dump_reactions_proj(db_session) == before

    # Now remove 👍 → the membership is gone; the skin-tone form is untouched.
    await insert_event(db_session, stream_id=stream, body=remove(_THUMB))
    emojis_after = set(
        (await db_session.execute(select(ReactionProj.emoji).where(ReactionProj.message_id == mid)))
        .scalars()
        .all()
    )
    assert emojis_after == {_THUMB_TONE, _CTRL}


# --- end to end ---------------------------------------------------------------


async def test_reaction_end_to_end(client: AsyncClient, db_session: AsyncSession) -> None:
    """A real batch: send → react → duplicate-react → react-to-nonexistent, then
    verify reactions_proj holds exactly one membership row for the real reaction."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)

    msg = message_body(auth=owner, stream_id=channel, text="hi")
    resp = await post_batch(client, owner["token"], [wire_item(msg)])
    assert resp.status_code == 200 and resp.json()["rejected"] == [], resp.text
    mid = msg["payload"]["message_id"]

    react1 = reaction_body(auth=owner, stream_id=channel, message_id=mid, emoji="\U0001f389")
    react2 = reaction_body(auth=owner, stream_id=channel, message_id=mid, emoji="\U0001f389")
    bad = reaction_body(
        auth=owner, stream_id=channel, message_id=ids.new_message_id(), emoji="\U0001f389"
    )
    resp = await post_batch(client, owner["token"], [wire_item(react1), wire_item(react2)])
    assert resp.status_code == 200 and resp.json()["rejected"] == [], resp.text
    assert len(resp.json()["accepted"]) == 2  # both stored (distinct event_ids)

    resp = await post_batch(client, owner["token"], [wire_item(bad)])
    body = resp.json()
    assert body["accepted"] == [] and len(body["rejected"]) == 1
    assert body["rejected"][0]["code"] == "unknown_message"

    # Exactly one membership row despite two reaction.added events (idempotent set).
    rows = await db_session.scalar(
        select(func.count()).select_from(ReactionProj).where(ReactionProj.message_id == mid)
    )
    assert rows == 1
