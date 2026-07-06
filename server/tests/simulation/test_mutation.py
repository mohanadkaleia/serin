"""Teeth / mutation check — prove the suite BITES a server write-path regression (§5).

Mirrors ENG-61's one-sided ``monkeypatch`` + ``undo`` + clean-positive-control shape,
against a **server insert seam**: patch a single write-path function to introduce a
specific defect, run one simulation example, assert an invariant fails, then undo and
re-run un-patched to confirm no false positive.

WHICH SEAM — and why not the plan's lean "candidate 1" (idempotency drop):
idempotency is enforced by a **DB** ``UNIQUE(workspace_id, event_id)`` constraint
(``models.Event.__table_args__``), so a Python monkeypatch of the upload router's
UNIQUE-catch cannot make a re-upload store a second row — it degrades to a
self-healing 500 that the cursors-are-truth invariants (which read DB truth + pulled,
never the upload ack) never observe.  So we take the plan's explicitly-sanctioned
**candidate 2 — the sequence-assignment write seam**: wrap ``insert_event`` with an
extra ``head_seq`` bump so per-stream sequences skip values (1, 3, 5, …).  This is a
real regression (a broken atomic sequence assignment) and deterministically breaks
**convergence/gaplessness** (invariant 2) — no gather-burst needed.  See the PR body
and the ENG-71 R1/mutation finding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from msgd.core.envelope import Envelope
from msgd.db.models import Stream
from msgd.events import insert as insert_module
from msgd.settings import Settings
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from simulation.runner import run_plan
from simulation.strategies import DuplicateSend, Plan, Send

#: A fixed example with ≥2 messages in a stream (so a sequence gap is observable)
#: plus a duplicate_send (exercises the idempotency path in the positive control)
#: and a private send (so the private stream also carries messages).
MUTATION_PLAN = Plan(
    n_members=2,
    ops=(
        Send(actor=0, stream=0, text="a"),
        Send(actor=1, stream=0, text="b"),
        DuplicateSend(actor=0, stream=0),
        Send(actor=0, stream=1, text="p"),
    ),
)


async def _buggy_insert_event(
    db: AsyncSession, *, stream_id: str, body: dict[str, Any]
) -> Envelope:
    """Correct insert, then an EXTRA ``head_seq`` bump — a broken sequence assigner.

    Each event still stores at ``head_seq + 1``, but the trailing extra bump makes
    the *next* event skip a value, so a stream's stored sequences become
    ``1, 3, 5, …`` — non-gapless.  Convergence's ``range(1, n+1)`` check bites.
    """
    envelope = await insert_module.insert_event(db, stream_id=stream_id, body=body)
    await db.execute(
        update(Stream).where(Stream.stream_id == stream_id).values(head_seq=Stream.head_seq + 1)
    )
    return envelope


def test_suite_detects_sequence_regression(
    settings: Settings, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-sided patch → an invariant fails; undo → the same example passes clean."""
    # emit.py binds ``insert_event`` by name at import, so patch it in emit's namespace.
    monkeypatch.setattr("msgd.events.emit.insert_event", _buggy_insert_event)
    # ``match`` pins the failure to the INTENDED invariant (convergence/gaplessness),
    # so a teeth test can never "pass" by tripping some incidental assertion.
    with pytest.raises(AssertionError, match="gapless"):
        asyncio.run(run_plan(settings, MUTATION_PLAN))

    # Clean positive control: no false-positive teeth — the same example passes.
    monkeypatch.undo()
    asyncio.run(run_plan(settings, MUTATION_PLAN))
