"""The §12 convergence property test — four invariants over randomized op sequences.

N (2–4) simulated writers + 1 adversary run a hypothesis-drawn op sequence against
one real in-process server (committing Postgres session), then flush + catch up; the
four §12-subset invariants (idempotency, convergence, cursor integrity, permission
isolation) are asserted after **every** example.  Under ``CI=true`` the ``ci`` profile
is derandomized with bounded ``max_examples`` (sized <2 min).

M2 EXTENSION SEAMS (documented, NOT built here):

* **Invariant 5 — pending settling:** optimistic-message ordering asserted at the
  client projection layer.  Seam: ``invariants.assert_pending_settling`` once the
  M2 web client's ``messages_proj`` lands.
* **Invariant 6 — rebuild equivalence:** drop projections + replay == incremental,
  client and server.  Seam: :func:`assert_convergence` compares *pulled event sets*
  today; M2 adds a rebuilt-projection comparison beside it (the §12 "byte-identical
  to a fresh rebuild-from-pull" clause gets teeth here).
* **Full six:** M1 asserts 1–4; the M2 gate flips to all six (TDD §13 M2 hard gate).
* **More op types** (edits, reactions, membership changes, DMs) → ``strategies``;
  **WS push transport** (ENG-68) → a second ``SimClient`` transport on the same
  cursor-truth state model.  The client/setup/strategies/invariants/runner split
  exists so each seam extends independently.
"""

from __future__ import annotations

import asyncio

from hypothesis import given
from msgd.settings import Settings

from simulation.runner import run_plan
from simulation.strategies import Plan, plans


@given(plan=plans())
def test_convergence_property(settings: Settings, migrated_db: str, plan: Plan) -> None:
    """Assert the four §12-subset invariants over a randomized op sequence.

    ``settings`` / ``migrated_db`` are session-scoped harness fixtures (container +
    one-time migration); ``plan`` is hypothesis-drawn.  Each example runs a full
    isolated world on the committing app inside its own event loop.
    """
    asyncio.run(run_plan(settings, plan))
