"""Apply an op sequence, settle, snapshot server truth, assert all four invariants.

The default driver is **randomized-sequential** (ops applied one at a time in the
hypothesis-drawn order) for CI-reproducible determinism; ``ConcurrentSendBurst`` is
the one op that fans out via ``asyncio.gather`` for the true streams-row-lock probe
(§3).  After the ops, every client reconnects, flushes its outbox, and catches up
(the §3.3 delivery contract); then server truth is read through a fresh committing
session and the four §12-subset invariants are asserted.

Lifecycle (R2): the sim commits real rows, so it starts from a truncated server and
``truncate_auth_tables`` runs in a ``finally`` — the ``test_events_batch_concurrency``
discipline, so committed rows never leak into other integration tests.
"""

from __future__ import annotations

import asyncio

from authutil import committing_app, truncate_auth_tables
from msgd.db.models import Event
from msgd.settings import Settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from simulation.invariants import Truth, assert_all
from simulation.setup import World, build_world
from simulation.strategies import (
    ConcurrentSendBurst,
    DisconnectMidFlush,
    DuplicateSend,
    Op,
    Plan,
    ReconnectCatchup,
    Send,
)


async def _apply_op(world: World, op: Op) -> None:
    """Dispatch one op against the world (sequential; bursts fan out internally)."""
    if isinstance(op, Send):
        actor = world.actors[op.actor]
        await actor.send(world.stream_id(op.stream), text=op.text)
        if actor.connected:
            await actor.flush()
    elif isinstance(op, DuplicateSend):
        actor = world.actors[op.actor]
        await actor.duplicate_send(world.stream_id(op.stream))
        if actor.connected:
            await actor.flush()
    elif isinstance(op, DisconnectMidFlush):
        actor = world.actors[op.actor]
        if not actor.outbox:
            await actor.send(world.public_stream)  # ensure a payload to lose the ack on
        actor.simulate_disconnect()
        await actor.flush()  # POST issued, ack discarded → items stay in the outbox
    elif isinstance(op, ReconnectCatchup):
        await world.actors[op.actor].reconnect()
    elif isinstance(op, ConcurrentSendBurst):
        stream = world.stream_id(op.stream)
        participants = [a for a in world.actors if a.connected][: op.count]
        for actor in participants:
            await actor.send(stream)
        # True concurrency: K simultaneous flushes to the SAME stream race on the
        # streams-row lock — the gaplessness/idempotency probe.
        await asyncio.gather(*(actor.flush() for actor in participants))


async def _settle(world: World) -> None:
    """Reconnect + flush every writer, catch up all readable streams, probe adversary."""
    for actor in world.actors:
        actor.connected = True
        await actor.flush()
    for actor in world.actors:
        for stream in await actor.sync():
            await actor.catchup_pull(stream)

    # Adversary probes for the permission-isolation invariant (every run).
    world.adversary.connected = True
    visible = await world.adversary.sync()
    world.adversary_visible = set(visible)
    for stream in visible:
        await world.adversary.catchup_pull(stream)
    # Direct existence probe: a private stream the adversary can't read → 404.
    readable = await world.adversary.catchup_pull(world.private_stream)
    world.adversary_private_forbidden = not readable


async def _snapshot_truth(world: World) -> Truth:
    """Read the stored event set per shared stream via a fresh committing session."""
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    truth: Truth = {}
    async with maker() as db:
        for stream in world.shared_streams:
            rows = (
                (
                    await db.execute(
                        select(Event)
                        .where(Event.stream_id == stream)
                        .order_by(Event.server_sequence)
                    )
                )
                .scalars()
                .all()
            )
            truth[stream] = [
                {
                    "event_id": row.event_id,
                    "server_sequence": row.server_sequence,
                    "event_hash": row.event_hash,
                    "body": row.body,
                    "stream_id": row.stream_id,
                }
                for row in rows
            ]
    return truth


async def run_plan(settings: Settings, plan: Plan) -> None:
    """Run one full example: bootstrap → apply ops → settle → assert all invariants.

    Raises ``AssertionError`` if any of the four §12-subset invariants fails (the
    teeth test relies on this).  Always truncates + disposes in ``finally`` (R2).
    """
    cleanup_engine = create_async_engine(settings.database_url)
    await truncate_auth_tables(cleanup_engine)  # start from an empty server (R2)

    http, engine = committing_app(settings)
    try:
        async with http:
            world = await build_world(http, engine, n_members=plan.n_members)
            for op in plan.ops:
                await _apply_op(world, op)
            await _settle(world)
            truth = await _snapshot_truth(world)
            assert_all(world, truth)
    finally:
        await truncate_auth_tables(cleanup_engine)
        await engine.dispose()
        await cleanup_engine.dispose()
