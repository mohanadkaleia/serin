"""Meta-event reducers — idempotent state mutations on ``streams``/``stream_members`` (ENG-65 D3).

A reducer is a **pure function of ``(event body dict, db state)``** that mutates
``streams``/``stream_members``, **runs in the same transaction as the event
insert**, and is **idempotent under replay**: ENG-66's rebuild replays the stored
log and re-runs reducers, so re-running any reducer over already-applied state
must be a no-op.  Creation is ``INSERT … ON CONFLICT DO NOTHING``; renames are
deterministic ``UPDATE``s; archival guards on ``archived_at IS NULL`` so a
re-apply does not churn the timestamp; removals are ``DELETE``.

Because the reducer owns **all** ``streams``/``stream_members`` creation, an
ENG-66 rebuild (re-run reducers over the stored log, no ``insert_event``)
reconstructs the full membership/stream state from event bodies alone — the
required replay property (D4).

``message.created`` has **no reducer** in ENG-65 (message projection is ENG-66+).
``bot.installed`` / ``bot.removed`` are M5 — not registered.  Types with no
reducer are dispatched as no-ops so the table is total.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.db.models import Stream, StreamMember

__all__ = ["Reducer", "REDUCERS", "apply_reducer"]

Reducer = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]


async def _insert_stream_if_absent(
    db: AsyncSession,
    *,
    stream_id: str,
    workspace_id: str,
    kind: str,
    name: str | None,
    visibility: str | None,
) -> None:
    """``INSERT … ON CONFLICT DO NOTHING`` a ``streams`` row (head_seq defaults 0)."""
    await db.execute(
        pg_insert(Stream)
        .values(
            stream_id=stream_id,
            workspace_id=workspace_id,
            kind=kind,
            name=name,
            visibility=visibility,
        )
        .on_conflict_do_nothing(index_elements=[Stream.stream_id])
    )


async def _add_member_if_absent(db: AsyncSession, *, stream_id: str, user_id: str) -> None:
    """``INSERT … ON CONFLICT DO NOTHING`` a ``stream_members`` row."""
    await db.execute(
        pg_insert(StreamMember)
        .values(stream_id=stream_id, user_id=user_id)
        .on_conflict_do_nothing(index_elements=[StreamMember.stream_id, StreamMember.user_id])
    )


# --- per-type reducers -------------------------------------------------------


async def _reduce_workspace_created(db: AsyncSession, body: dict[str, Any]) -> None:
    """Ensure the workspace-meta stream row exists (keyed on ``body.stream_id``)."""
    await _insert_stream_if_absent(
        db,
        stream_id=body["stream_id"],
        workspace_id=body["workspace_id"],
        kind="workspace-meta",
        name=None,
        visibility=None,
    )


async def _reduce_noop(db: AsyncSession, body: dict[str, Any]) -> None:
    """No streams/members effect.

    ``user.joined`` / ``user.left`` / ``user.profile_updated``: the ``users`` row
    is authored by the auth handler, and workspace-meta readability is by
    workspace role, not a ``stream_members`` row (D5).  Registered as an explicit
    no-op so the dispatch table is total.
    """
    return None


async def _reduce_channel_created(db: AsyncSession, body: dict[str, Any]) -> None:
    """Create the channel's own stream row + subscribe the creator (D3/D4).

    The channel's **own** stream row (``payload.channel_stream_id``, ``head_seq``
    defaults 0) is created here regardless of where the genesis event is homed —
    §2.2 privacy placement (public → workspace-meta; private → the channel's own
    stream seq 1) is decided by the caller/uploader, not the reducer.
    """
    payload = body["payload"]
    await _insert_stream_if_absent(
        db,
        stream_id=payload["channel_stream_id"],
        workspace_id=body["workspace_id"],
        kind="channel",
        name=payload["name"],
        visibility=payload["visibility"],
    )
    await _add_member_if_absent(
        db, stream_id=payload["channel_stream_id"], user_id=body["author_user_id"]
    )


async def _reduce_channel_renamed(db: AsyncSession, body: dict[str, Any]) -> None:
    payload = body["payload"]
    await db.execute(
        update(Stream)
        .where(Stream.stream_id == payload["channel_stream_id"])
        .values(name=payload["name"])
    )


async def _reduce_channel_archived(db: AsyncSession, body: dict[str, Any]) -> None:
    """Mark the channel archived (writes/UI gate only; history stays readable, D13).

    Guarded on ``archived_at IS NULL`` so a replay does not churn the timestamp
    — keeping the reducer idempotent under re-apply.
    """
    payload = body["payload"]
    await db.execute(
        update(Stream)
        .where(Stream.stream_id == payload["channel_stream_id"], Stream.archived_at.is_(None))
        .values(archived_at=func.now())
    )


async def _reduce_channel_member_added(db: AsyncSession, body: dict[str, Any]) -> None:
    payload = body["payload"]
    await _add_member_if_absent(
        db, stream_id=payload["channel_stream_id"], user_id=payload["user_id"]
    )


async def _reduce_channel_member_removed(db: AsyncSession, body: dict[str, Any]) -> None:
    payload = body["payload"]
    await db.execute(
        delete(StreamMember).where(
            StreamMember.stream_id == payload["channel_stream_id"],
            StreamMember.user_id == payload["user_id"],
        )
    )


async def _reduce_dm_created(db: AsyncSession, body: dict[str, Any]) -> None:
    """Create the DM stream + one member row per participant (reducer ready, D3).

    No DM-creation endpoint ships in ENG-65 (M3 lazy-on-first-message); the
    reducer + predicate support exist so the machinery is ready.
    """
    payload = body["payload"]
    await _insert_stream_if_absent(
        db,
        stream_id=payload["dm_stream_id"],
        workspace_id=body["workspace_id"],
        kind="dm",
        name=None,
        visibility=None,
    )
    for user_id in payload["member_user_ids"]:
        await _add_member_if_absent(db, stream_id=payload["dm_stream_id"], user_id=user_id)


#: Reducer registry keyed by event ``type``.  Types absent here have no reducer
#: (``message.created`` — ENG-66+; ``bot.*`` — M5) and dispatch as a no-op.
REDUCERS: dict[str, Reducer] = {
    "workspace.created": _reduce_workspace_created,
    "user.joined": _reduce_noop,
    "user.left": _reduce_noop,
    "user.profile_updated": _reduce_noop,
    "channel.created": _reduce_channel_created,
    "channel.renamed": _reduce_channel_renamed,
    "channel.archived": _reduce_channel_archived,
    "channel.member_added": _reduce_channel_member_added,
    "channel.member_removed": _reduce_channel_member_removed,
    "dm.created": _reduce_dm_created,
}


async def apply_reducer(db: AsyncSession, body: dict[str, Any]) -> None:
    """Dispatch ``body`` to its reducer; no-op for types with no reducer."""
    reducer = REDUCERS.get(body["type"])
    if reducer is None:
        return None
    await reducer(db, body)
