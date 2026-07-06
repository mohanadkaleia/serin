"""The four §12-subset skeleton invariants, asserted **every** run (§2).

The full six land at M2; §12 invariants 5 (pending settling) and 6 (rebuild
equivalence) are documented seams (see ``test_simulation`` docstring), not asserted
here.  Each ``assert_*`` is a small pure function over the clients + a server-truth
snapshot; :func:`assert_all` runs all four after every hypothesis example.

``Truth`` is the server's stored event set per shared stream, ascending
``server_sequence`` — read by the runner through a fresh committing session
(direct ``select(Event)``), exactly as ``test_events_batch_concurrency`` does.
"""

from __future__ import annotations

from typing import Any

from msgd.core.hashing import hash_event

from simulation.client import SimClient
from simulation.setup import World

#: One stored event, projected to the fields the invariants compare.
TruthEvent = dict[str, Any]
#: stream_id -> ascending list of stored events.
Truth = dict[str, list[TruthEvent]]


def assert_idempotency(world: World, truth: Truth) -> None:
    """§12.1 — no duplicate ``event_id``; exactly one stored row per intended send.

    Retried (:meth:`SimClient.flush` re-POSTs) and duplicated
    (:meth:`SimClient.duplicate_send`) uploads must each collapse to a single
    stored event — proving the server's ``UNIQUE(workspace_id, event_id)`` +
    idempotent re-accept holds under the dumb-outbox retry loop.
    """
    for stream in world.shared_streams:
        events = truth[stream]
        event_ids = [e["event_id"] for e in events]
        assert len(event_ids) == len(set(event_ids)), f"duplicate event_id stored in {stream}"

        stored_messages = {
            e["event_id"] for e in events if e["body"].get("type") == "message.created"
        }
        intended = {
            eid for actor in world.actors for eid, sid in actor.intended.items() if sid == stream
        }
        assert stored_messages == intended, (
            f"idempotency: stored message set != intended in {stream}; "
            f"stored={stored_messages!r} intended={intended!r}"
        )


def assert_convergence(world: World, truth: Truth) -> None:
    """§12.2 (subset) — every member's ``pulled`` == server truth == each other.

    Byte-equal envelopes (``body`` dict, ``event_hash``, ``server_sequence`` all
    equal) and gapless (``[server_sequence] == range(1, n+1)``).  ``hash_event(body)
    == event_hash`` is re-checked on the stored body as the byte-equality anchor.
    (M1 compares pulled event sets; the rebuilt-projection half is invariant 6, M2.)
    """
    for stream in world.shared_streams:
        events = truth[stream]
        seqs = [e["server_sequence"] for e in events]
        assert seqs == list(range(1, len(events) + 1)), (
            f"convergence: server truth not gapless in {stream}: {seqs}"
        )
        for e in events:
            assert hash_event(e["body"]) == e["event_hash"], (
                f"convergence: stored body does not hash to its event_hash in {stream}"
            )

        for actor in world.actors:
            pulled = actor.pulled.get(stream, [])
            assert len(pulled) == len(events), (
                f"convergence: {actor.user_id} pulled {len(pulled)} events, "
                f"server has {len(events)} in {stream}"
            )
            for got, want in zip(pulled, events, strict=True):
                assert got["server"]["server_sequence"] == want["server_sequence"], (
                    f"convergence: sequence mismatch in {stream} for {actor.user_id}"
                )
                assert got["event_hash"] == want["event_hash"], (
                    f"convergence: event_hash mismatch in {stream} for {actor.user_id}"
                )
                assert got["body"] == want["body"], (
                    f"convergence: body not byte-equal in {stream} for {actor.user_id}"
                )


def assert_cursor_integrity(world: World, truth: Truth) -> None:
    """§12.3 — each client's per-stream ``pulled`` sequence is gapless + dup-free.

    After arbitrary disconnect / reconnect / missed events, the reconnect catch-up
    recovered exactly the missed tail: strictly-ascending contiguous
    ``server_sequence`` with no repeats, and the cursor equals the last pulled seq.
    Checked for every client, adversary included (its public pull must be clean).
    """
    clients: list[SimClient] = [*world.actors, world.adversary]
    for client in clients:
        for stream, pulled in client.pulled.items():
            seqs = [e["server"]["server_sequence"] for e in pulled]
            assert len(seqs) == len(set(seqs)), (
                f"cursor: duplicate sequence for {client.user_id} in {stream}: {seqs}"
            )
            assert seqs == sorted(seqs), (
                f"cursor: non-ascending sequence for {client.user_id} in {stream}: {seqs}"
            )
            if seqs:
                assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), (
                    f"cursor: gap for {client.user_id} in {stream}: {seqs}"
                )
            assert client.cursors.get(stream, 0) == (seqs[-1] if seqs else 0), (
                f"cursor: cursor != last pulled seq for {client.user_id} in {stream}"
            )


def assert_permission_isolation(world: World) -> None:
    """§12.4 — the adversary observes ZERO private-stream data (ACCEPTANCE, every run).

    The adversary is a workspace member but a non-member of the private channel.
    (a) the private stream is **absent** from its ``GET /v1/sync``; (b) a direct
    ``GET /v1/events?stream_id=<private>`` returned **404** (existence not
    disclosed, §3.6.2 — not 403); (c) its ``pulled`` contains no event whose
    ``body.stream_id`` is the private channel.  (a)/(b) are collected during settle.
    """
    priv = world.private_stream
    assert priv not in world.adversary_visible, (
        "isolation: adversary's sync exposed the private stream"
    )
    assert world.adversary_private_forbidden, "isolation: adversary's private pull was not a 404"
    for stream, pulled in world.adversary.pulled.items():
        assert stream != priv or not pulled, "isolation: adversary pulled the private stream"
        for ev in pulled:
            assert ev["body"].get("stream_id") != priv, (
                "isolation: a private-stream event reached the adversary's pulled log"
            )


def assert_all(world: World, truth: Truth) -> None:
    """Run all four skeleton invariants (called after every example)."""
    assert_idempotency(world, truth)
    assert_convergence(world, truth)
    assert_cursor_integrity(world, truth)
    assert_permission_isolation(world)
