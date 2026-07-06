"""WebSocket fanout seam (ENG-66 ships the seam; ENG-68 fills the body).

:func:`publish_event` is a module-level async callable the upload router invokes
**after each per-event commit, exactly once per newly accepted event** — never on
the idempotent re-accept path (a re-upload of an already-stored ``event_id`` was
already published on its first acceptance, D7/D9).

It is a deliberate no-op here so the M1 write path is complete without a live WS
hub. ENG-68 replaces the body with permission-scoped fanout via the in-memory
connection registry **without touching the router loop** — the router depends
only on this name and its post-commit call site, keeping the two tickets from
colliding on ``ws/`` files.
"""

from __future__ import annotations

from msgd.core.envelope import Envelope

__all__ = ["publish_event"]


async def publish_event(envelope: Envelope) -> None:
    """No-op WS fanout seam (ENG-68 replaces the body).

    Invoked by the upload router after each per-event commit, once per newly
    accepted event (never on the idempotent re-accept path).
    """
    return None
