"""Incremental ``messages_proj`` apply — the single source of projection truth (ENG-69).

``apply_projection`` is the one function BOTH the incremental accept path
(:mod:`msgd.events.insert` step 3b) and the full :mod:`msgd.projections.rebuild`
replay call.  Because there is exactly one apply implementation, ``rebuild ≡
incremental`` holds **by construction** — a rebuild replays the stored log
through the same handler the accept path used (the Postgres analogue of the M0
SQLite ``project``/``rebuild`` both calling ``_apply_message_created``).

Dispatch mirrors ``reducers.REDUCERS`` and the M0 ``projection._HANDLERS``, but
is keyed on ``(type, type_version)`` (a version bump is a new, unhandled key —
D9-skipped, never a silent mis-apply).  Handlers exist for ``("message.created",
1)`` (→ ``messages_proj``) and, since ENG-97 (M3), ``("reaction.added", 1)`` /
``("reaction.removed", 1)`` (→ the ``reactions_proj`` set).  Everything else —
meta events, unknown types, and any ``v>=2`` — has no handler and is a **no-op**
(D9: skipped in the projection, never crashes).  ``bot.*``/edits/deletes are
later milestones; their ``messages_proj`` columns exist but no reducer writes them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.core.payloads import MessageCreatedV1, ReactionAddedV1, ReactionRemovedV1
from msgd.db.models import MessageProj, ReactionProj

__all__ = ["PROJECTION_VERSION", "apply_projection"]

#: The projection contract version. It governs BOTH the ``messages_proj`` shape
#: AND the apply logic: bump it on ANY change to either — add/drop a projected
#: column, add or change a handler, or change how a field maps.
#:
#: M1 declares the version (satisfying the "every projection declares its
#: version" D-invariant, TDD §2.3 rule 5) but deliberately does NOT ship the
#: stored-version auto-rebuild-on-mismatch machinery that the M0 SQLite/Dexie
#: sides carry: a server version bump is handled by an operator running
#: ``msgctl rebuild-projections``.  Deferred until a bump actually needs it
#: (ENG-69 plan Ruling / risk 5).
#:
#: Bumped to 2 by ENG-97 (M3): the reaction handlers + the new ``reactions_proj``
#: shape are a change to both the projection tables AND the apply logic — the
#: exact "bump on ANY change to either" trigger above. An operator upgrading to
#: M3 runs ``msgctl rebuild-projections`` once to materialize ``reactions_proj``.
PROJECTION_VERSION: Final = 2

_Handler = Callable[..., Awaitable[None]]


async def _apply_message_created(
    db: AsyncSession, *, body: dict[str, Any], server_sequence: int
) -> None:
    """Project one ``message.created`` v1 event into ``messages_proj``.

    Re-validates the opaque ``payload`` through :class:`MessageCreatedV1`
    (defence-in-depth: ENG-66's ``validate.py`` already validated it pre-accept)
    and ``INSERT … ON CONFLICT (message_id) DO NOTHING`` — the Postgres analogue
    of M0's ``INSERT OR IGNORE``.  ``message.created`` is **immutable** in M1
    (edits/deletes are later milestones), so an existing row and a re-applied one
    are byte-identical; ``DO NOTHING`` keeps the incremental accept and the
    rebuild replay in agreement on any re-seen ``message_id`` and makes replay
    idempotent.

    Only the columns the apply owns are written:

    * ``message_id`` / ``text`` / ``thread_root_id`` — from the validated payload.
    * ``stream_id`` / ``author_user_id`` — from the envelope ``body``.
    * ``created_seq`` — the accept-time ``server_sequence``.

    Everything else DEFAULTS: ``edited_seq``/``last_reply_seq`` NULL, ``deleted``
    FALSE, ``reply_count`` 0 (edits/deletes/thread counters are a later milestone
    — the columns exist, no reducer touches them now).  ``search_tsv`` is a
    GENERATED column (a pure function of ``text``) and is never written.
    """
    payload = MessageCreatedV1(**body["payload"])
    await db.execute(
        pg_insert(MessageProj)
        .values(
            message_id=payload.message_id,
            stream_id=body["stream_id"],
            thread_root_id=payload.thread_root_id,
            author_user_id=body["author_user_id"],
            text=payload.text,
            created_seq=server_sequence,
        )
        .on_conflict_do_nothing(index_elements=[MessageProj.message_id])
    )


async def _apply_reaction_added(
    db: AsyncSession, *, body: dict[str, Any], server_sequence: int
) -> None:
    """Project one ``reaction.added`` v1 event: idempotent set-insert (§2.4).

    Re-validates the opaque ``payload`` through :class:`ReactionAddedV1`
    (defence-in-depth) and ``INSERT … ON CONFLICT DO NOTHING`` on the membership
    key ``(message_id, author_user_id, emoji)``. Already-present membership is a
    **no-op** — the count (``= |{author_user_id}|`` for a ``(message_id, emoji)``)
    is unchanged, exactly the §2.4 idempotent-add semantics. A duplicate
    ``reaction.added`` (same key, different ``event_id``) sequences as its own
    event but lands zero new rows, so the projected set — hence the aggregated
    count — is a pure function of the log. ``author_user_id`` comes from the
    envelope (§2.4 keys the set on it, not the payload). ``emoji`` is bound as an
    opaque parameter into the ``COLLATE "C"`` byte-exact column.
    """
    payload = ReactionAddedV1(**body["payload"])
    await db.execute(
        pg_insert(ReactionProj)
        .values(
            message_id=payload.message_id,
            author_user_id=body["author_user_id"],
            emoji=payload.emoji,
        )
        .on_conflict_do_nothing(
            index_elements=[
                ReactionProj.message_id,
                ReactionProj.author_user_id,
                ReactionProj.emoji,
            ]
        )
    )


async def _apply_reaction_removed(
    db: AsyncSession, *, body: dict[str, Any], server_sequence: int
) -> None:
    """Project one ``reaction.removed`` v1 event: idempotent set-delete (§2.4).

    ``DELETE`` the membership key ``(message_id, author_user_id, emoji)``.
    Removing an absent reaction deletes zero rows — a **no-op**, exactly the §2.4
    idempotent-remove semantics — so a ``reaction.removed`` for a reaction that
    was never added (or already removed) sequences as its own event yet leaves the
    set unchanged. ``emoji`` matches byte-exactly via the column's ``C`` collation.
    """
    payload = ReactionRemovedV1(**body["payload"])
    await db.execute(
        delete(ReactionProj).where(
            ReactionProj.message_id == payload.message_id,
            ReactionProj.author_user_id == body["author_user_id"],
            ReactionProj.emoji == payload.emoji,
        )
    )


#: The projection's dispatch table, keyed ``(type, type_version)`` — distinct
#: from ``core``'s payload-validation registry and from ``reducers.REDUCERS``
#: (which is ``type``-keyed).  In M1 exactly one handler exists; every other
#: ``(type, version)`` — meta events, unknown types, ``message.created`` v>=2 —
#: has no handler and is uniformly skipped (D9).
_HANDLERS: Final[dict[tuple[str, int], _Handler]] = {
    ("message.created", 1): _apply_message_created,
    ("reaction.added", 1): _apply_reaction_added,
    ("reaction.removed", 1): _apply_reaction_removed,
}


async def apply_projection(db: AsyncSession, *, body: dict[str, Any], server_sequence: int) -> bool:
    """Apply one event ``body`` to ``messages_proj`` (no commit — caller's txn).

    Dispatches on ``(body["type"], body["type_version"])``.  Returns ``True`` if
    a handler ran (the event projects to a row), ``False`` if it was a D9 no-op
    (meta / unknown type / unhandled version) — the rebuild uses the flag for its
    applied/skipped summary.  Never commits; runs inside the caller's
    transaction, so a raise here rolls the caller's txn back (the accept-path
    "projection failure rejects the event" guarantee, ENG-69 Pin 5).

    :class:`~pydantic.ValidationError` from a handler propagates: on a
    pre-validated payload it is impossible, so surfacing it loudly (accept path →
    500) is preferable to silently letting ``events`` and ``messages_proj``
    diverge (ENG-69 Pin 5 — loud-is-preferable-to-silent-divergence).
    """
    handler = _HANDLERS.get((body["type"], body["type_version"]))
    if handler is None:
        return False  # D9: meta / unknown type / message.created v>=2 → skip, never crash
    await handler(db, body=body, server_sequence=server_sequence)
    return True
