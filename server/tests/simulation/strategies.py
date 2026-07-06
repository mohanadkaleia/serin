"""Hypothesis op-generation + the interleaving/concurrency model (§3).

The op union is a small set of frozen dataclasses; a :class:`Plan` is a member
count ``n`` (2–4) plus a bounded list of ops.  ``actor`` indexes the writer list
(owner + invited members, adversary excluded); ``stream`` is ``0`` (public) or
``1`` (private).

**Interleaving model — RULED:** randomized-*sequential* application with occasional
gather-bursts.  The runner applies ops one at a time in the hypothesis-drawn order
(randomized interleaving of client actions, yet deterministic and CI-reproducible
— no wall-clock races).  The *one* op that needs true concurrency —
``ConcurrentSendBurst`` — issues K simultaneous sends to the **same** stream via
``asyncio.gather`` (the ``test_events_batch_concurrency`` pattern), the targeted
streams-row-lock / gaplessness probe.

**Profiles:** ``ci`` = ``derandomize=True`` + bounded ``max_examples`` +
``deadline=None`` (container IO), sized <2 min; ``dev`` = more examples,
non-derandomized.  ``ci`` is selected when ``CI`` is set (the harness threads
``CI: "true"``).  Registered at import so both suite modules pick up the profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from hypothesis import HealthCheck
from hypothesis import settings as hp_settings
from hypothesis import strategies as st

# --- op union -----------------------------------------------------------------


@dataclass(frozen=True)
class Send:
    """``actor`` mints + flushes one message to ``stream``."""

    actor: int
    stream: int
    text: str


@dataclass(frozen=True)
class DuplicateSend:
    """``actor`` re-enqueues its last message (same ``event_id``) → dup upload."""

    actor: int
    stream: int


@dataclass(frozen=True)
class DisconnectMidFlush:
    """``actor`` disconnects and flushes: the POST is issued but the ack is lost."""

    actor: int


@dataclass(frozen=True)
class ReconnectCatchup:
    """``actor`` reconnects: flush the outbox, then sync + catch up every stream."""

    actor: int


@dataclass(frozen=True)
class ConcurrentSendBurst:
    """K writers send to the **same** ``stream`` via ``asyncio.gather`` (true race)."""

    stream: int
    count: int


Op = Send | DuplicateSend | DisconnectMidFlush | ReconnectCatchup | ConcurrentSendBurst


@dataclass(frozen=True)
class Plan:
    """One hypothesis example: member count + a bounded op sequence."""

    n_members: int
    ops: tuple[Op, ...]


# --- bounds (tuned for the <2 min CI budget, R3) ------------------------------

MIN_MEMBERS, MAX_MEMBERS = 2, 4
MAX_OPS = 10
MAX_BURST = 3
_TEXTS = ("a", "hi", "msg", "z9", "hello")


def _op(n: int) -> st.SearchStrategy[Op]:
    """The op union for a world with ``n`` writers."""
    actor = st.integers(min_value=0, max_value=n - 1)
    stream = st.integers(min_value=0, max_value=1)
    text = st.sampled_from(_TEXTS)
    return st.one_of(
        st.builds(Send, actor=actor, stream=stream, text=text),
        st.builds(DuplicateSend, actor=actor, stream=stream),
        st.builds(DisconnectMidFlush, actor=actor),
        st.builds(ReconnectCatchup, actor=actor),
        st.builds(
            ConcurrentSendBurst,
            stream=stream,
            count=st.integers(min_value=2, max_value=MAX_BURST),
        ),
    )


@st.composite
def plans(draw: st.DrawFn) -> Plan:
    """Draw a :class:`Plan`: ``n`` writers then a bounded op sequence over them."""
    n = draw(st.integers(min_value=MIN_MEMBERS, max_value=MAX_MEMBERS))
    ops = draw(st.lists(_op(n), min_size=0, max_size=MAX_OPS))
    return Plan(n_members=n, ops=tuple(ops))


# --- hypothesis profiles ------------------------------------------------------

_SUPPRESSED = [HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
_registered = False


def register_profiles() -> None:
    """Register + load the ci/dev profiles (idempotent)."""
    global _registered
    if _registered:
        return
    hp_settings.register_profile(
        "ci",
        hp_settings(
            max_examples=25, derandomize=True, deadline=None, suppress_health_check=_SUPPRESSED
        ),
    )
    hp_settings.register_profile(
        "dev",
        hp_settings(max_examples=50, deadline=None, suppress_health_check=_SUPPRESSED),
    )
    hp_settings.load_profile("ci" if os.environ.get("CI") else "dev")
    _registered = True


register_profiles()
