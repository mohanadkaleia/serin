"""``emit_event`` — the server-authored meta-event orchestrator (ENG-65 D4).

``emit_event`` runs the reducer **BEFORE** ``insert_event`` in one transaction.
This ordering is the load-bearing **private-channel bootstrap invariant** — do
not swap it:

    insert_event locks the ``streams`` row to assign a sequence, so the row must
    exist *before* the first event is sequenced into it.  For a private channel
    (and a DM), §2.2 homes the genesis ``channel.created`` / ``dm.created`` event
    in **the new stream's own stream at sequence 1** (self-describing).  The
    stream cannot host its own genesis event unless its row already exists — so
    the reducer (which idempotently creates the row at ``head_seq=0``) MUST run
    first.  Reversing to insert-then-reduce breaks private-channel genesis.

The one ordering is uniform and correct for every meta type:

* ``workspace.created`` → reducer ensures the workspace-meta row; insert → seq 1.
* Public ``channel.created`` → home = workspace-meta (exists); reducer creates the
  channel's *own separate* stream row (head_seq=0) + creator membership; insert
  appends to workspace-meta's sequence.
* Private ``channel.created`` → home == the channel's own stream; reducer creates
  that row (head_seq=0); insert → seq 1 in the channel's own stream.
* ``dm.created`` → home == the DM stream; reducer creates the DM row + members;
  insert → seq 1 in the DM stream.
* ``channel.member_added/removed`` / ``renamed`` / ``archived`` → home already
  exists; order is immaterial; reducer mutates membership/name.

§2.2 privacy placement (which stream a lifecycle event lands in) is decided by
the caller, not the reducer.  ENG-65 encodes it only in the two server-authored
callers (``/v1/setup``, ``/v1/auth/accept-invite``) and documents it for ENG-66.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from msgd.core.envelope import Envelope
from msgd.events.insert import insert_event
from msgd.events.reducers import apply_reducer

__all__ = ["emit_event"]


async def emit_event(db: AsyncSession, *, home_stream_id: str, body: dict[str, Any]) -> Envelope:
    """Reducer-before-insert (D4) — the private-channel bootstrap invariant.

    Runs inside the caller's transaction and does not commit.  Do not reorder:
    the reducer must create/ensure the ``streams`` rows (``head_seq`` stays 0)
    *before* :func:`insert_event` locks the now-existing home row to assign the
    sequence.
    """
    # BOOTSTRAP INVARIANT (D4): reducer FIRST (ensures streams rows + members),
    # THEN insert (locks the now-existing row → seq). Never swap these.
    await apply_reducer(db, body)
    return await insert_event(db, stream_id=home_stream_id, body=body)
