"""Dogfood-scale exit gate — CI-permanent, deterministic (ENG-62).

Builds a synthetic workspace of ~5000 ``message.created`` events across 8
streams (with unicode text and a handful of unknown-type injections so the D9
skip path is exercised at scale), then proves the M0 exit criteria hold at
scale:

1. **rebuild ≡ incremental** — one ``project()`` pass over the whole log
   (cursor 0→head) and a full :func:`~msgctl.rebuild.rebuild_projection`
   produce byte-identical :func:`~msgctl.projection.dump_messages`.
2. **verify green** — :func:`msgctl.verify.verify_workspace` exits 0 on the
   dogfood-sized workspace (the checklist's "verify green on a dogfood-sized
   synthetic workspace" item).

Deterministic and NOT hypothesis: a fixed ``random.Random(seed)`` distributes a
FIXED count of events across the streams, so the runtime is bounded and the
test doubles as a perf canary. It is kept in the normal CI pytest run (never
skipped, no ``slow`` marker) so a regression in projection/rebuild/verify at
scale — or a runtime creep past budget — surfaces in CI.

**Measured runtime (local macOS, first run): ~30s** for 5000 events + one
project + one rebuild + one verify — under the 45s soft / 60s hard budget.
``append_event`` fsyncs per event, which dominates the wall clock on macOS; on
Linux CI the fsync is sub-millisecond, so this lands well under budget there.
Because local runtime stays under 45s the count stays at 5000 (the plan's trim
threshold was not hit).

Built via library calls (same helpers as ``test_equivalence_gate.py``) — never
a subprocess per send, which would blow the CI time budget.
"""

from __future__ import annotations

import random
from pathlib import Path

from msgctl import verify
from msgctl.append import append_event
from msgctl.projection import PROJECTION_DB_NAME, dump_messages, open_db, project
from msgctl.rebuild import rebuild_projection
from msgctl.workspace import (
    Workspace,
    init_workspace,
    now_rfc3339,
    resolve_or_create_stream,
)
from msgd.core import ids
from msgd.core.envelope import Body, Envelope, ServerMetadata
from msgd.core.hashing import hash_event
from msgd.core.payloads import build_message_created_body

#: Fixed scale (see the module docstring's runtime note). 8 streams; ~1/50 of
#: the events are unknown-type injections exercising the D9 skip path at scale.
_N_EVENTS = 5000
_N_STREAMS = 8
_SEED = 0xE62

#: Varied text incl. unicode (emoji, CJK, combining marks) so the projection
#: stores non-ASCII at scale via ``ensure_ascii=False``.
_TEXTS = [
    "hello everyone",
    "wörld — ünïcode ✓ 🌍",
    "第三条消息",
    "",  # empty text is valid
    "café ünïcö 世界",
    "plain five",
    "é combining acute",
    "multi\nline\ttext",
]


def _send_created(ws: Workspace, stream_id: str, text: str, fmt: str) -> None:
    def build_envelope(server_sequence: int, server_received_at: str) -> Envelope:
        body = build_message_created_body(
            workspace_id=ws.workspace_id,
            stream_id=stream_id,
            author_user_id=ws.local_author.user_id,
            author_device_id=ws.local_author.device_id,
            client_created_at=now_rfc3339(),
            text=text,
            format=fmt,
        )
        return Envelope(
            body=body,
            event_hash=hash_event(body.model_dump(mode="json")),
            signature=None,
            server=ServerMetadata(
                server_sequence=server_sequence,
                server_received_at=server_received_at,
                payload_redacted=False,
            ),
        )

    append_event(ws, stream_id, build_envelope=build_envelope)


def _send_unknown(ws: Workspace, stream_id: str) -> None:
    """Unknown-type event (D9): projection must skip it, verify must not flag it."""

    def build_envelope(server_sequence: int, server_received_at: str) -> Envelope:
        body = Body(
            event_id=ids.new_event_id(),
            workspace_id=ws.workspace_id,
            stream_id=stream_id,
            type="widget.exploded",
            type_version=7,
            author_user_id=ws.local_author.user_id,
            author_device_id=ws.local_author.device_id,
            client_created_at=now_rfc3339(),
            payload={"blast_radius": 3},
        )
        return Envelope(
            body=body,
            event_hash=hash_event(body.model_dump(mode="json")),
            signature=None,
            server=ServerMetadata(
                server_sequence=server_sequence,
                server_received_at=server_received_at,
                payload_redacted=False,
            ),
        )

    append_event(ws, stream_id, build_envelope=build_envelope)


def test_dogfood_scale_rebuild_equivalence_and_verify(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace.open(root)

    stream_ids = [resolve_or_create_stream(ws, f"s{i}") for i in range(_N_STREAMS)]
    rng = random.Random(_SEED)

    for _ in range(_N_EVENTS):
        stream_id = stream_ids[rng.randrange(_N_STREAMS)]
        if rng.randrange(50) == 0:
            _send_unknown(ws, stream_id)
        else:
            _send_created(ws, stream_id, rng.choice(_TEXTS), rng.choice(["markdown", "plain"]))

    # Incremental: one project() pass over the whole log (cursor 0→head).
    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        result = project(ws, conn)
        assert result.applied + result.skipped == _N_EVENTS
        assert result.skipped > 0  # D9 unknown-type injections were skipped, not crashed
        dump_incremental = dump_messages(conn)
    finally:
        conn.close()  # MUST close before rebuild swaps the live DB file.

    # Full rebuild (temp DB + atomic swap), then re-read.
    rebuild_projection(ws)
    conn2 = open_db(root / PROJECTION_DB_NAME)
    try:
        dump_rebuilt = dump_messages(conn2)
    finally:
        conn2.close()

    # rebuild ≡ incremental at scale.
    assert dump_rebuilt == dump_incremental

    # verify green on the dogfood-sized synthetic workspace (checklist item 4).
    assert verify.verify_workspace(root).exit_code == 0
