"""M1 EXIT GATE (ENG-73): two ``msgctl`` clients converge under INTERLEAVED
BIDIRECTIONAL traffic over the real server.

This is the M1 analogue of the M0 ENG-61 convergence gate. Where
``test_remote_e2e.py`` is a *feature* test (does login/push/pull/invite work end
to end), this is the *convergence* gate: two independent authors — two "devices",
workspaces A and B — both write into the **same** public ``general`` channel
across **multiple push rounds**, with sends interleaved and server-arrival order
varied per round, then are driven to a **fixpoint** (the final pull is a no-op).
The property under test is server-order-agnostic: after quiescence the two clients
hold byte-equal logs and byte-equal projections regardless of how the single
gapless per-stream sequence interleaved the two authors.

The real-server mechanism (Postgres testcontainer + subprocess ``uvicorn`` on the
true ASGI app) and the ``_run``/``_log_lines``/``_project_dump`` helpers are shared
with ``test_remote_e2e.py`` via ``_e2e_server``. One container, one server-process
shape, amortized across this module.

Perf canary (ENG-62 discipline): ``ROUNDS`` × ``K`` per author = the interleave
size. Currently 3 × 4, so 2 × 3 × 4 = 24 ``message.created`` appends over the real
stack. Runtime target < 60 s (one container amortized). If the interleave count
pushes runtime, trim ``K`` and record the new number here.

Marked ``integration`` (needs Docker); ``-m "not integration"`` skips it.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from _e2e_server import _log_lines, _project_dump, _run
from _e2e_server import live_server as live_server  # shared fixture re-export
from msgctl import credentials, outbox
from msgctl.cli import main
from msgctl.projection import PROJECTION_DB_NAME, dump_messages, open_db
from msgctl.rebuild import rebuild_projection
from msgctl.workspace import Workspace
from msgd.core.hashing import hash_event
from msgd.core.payloads import build_message_created_body

pytestmark = pytest.mark.integration

OWNER_PASSWORD = "correct-horse-battery-staple"
MEMBER_PASSWORD = "another-valid-password-42"

#: Interleave size (perf canary — see module docstring).
ROUNDS = 3
K = 4


def _stream_line_counts(root: Path) -> dict[str, int]:
    """Map ``stream_id -> stored ndjson line count`` for every synced stream."""
    return {d.name: len(_log_lines(d)) for d in (root / "streams").iterdir() if d.is_dir()}


def _nonempty_lines(text: str) -> int:
    return len([ln for ln in text.splitlines() if ln.strip()])


def test_two_devices_converge_interleaved_bidirectional(
    live_server: tuple[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_url, server_log = live_server
    ws_a = tmp_path / "device_a"
    ws_b = tmp_path / "device_b"
    out: list[str] = []

    # --- Setup: A owns "Acme"; the setup path creates the public `general` -----
    _run(
        capsys,
        out,
        "login",
        str(ws_a),
        "--setup",
        "--server-url",
        base_url,
        "--email",
        "owner@example.com",
        "--password",
        OWNER_PASSWORD,
        "--workspace-name",
        "Acme",
        "--display-name",
        "Owner",
    )
    # A seeds `general` with one message so the channel exists server-side, then
    # pushes — B will resolve it from `/v1/sync`. Capture it for the A5 replay.
    a_seed = json.loads(
        _run(capsys, out, "send", str(ws_a), "--stream", "general", "--text", "a-seed")
    )
    _run(capsys, out, "push", str(ws_a))

    # --- A mints a `member` invite; B joins and pulls -------------------------
    invite_out = json.loads(_run(capsys, out, "invite", str(ws_a), "--role", "member"))
    token = invite_out["url"].rsplit("/join/", 1)[1]
    _run(
        capsys,
        out,
        "login",
        str(ws_b),
        "--invite-token",
        token,
        "--server-url",
        base_url,
        "--email",
        "bob@example.com",
        "--password",
        MEMBER_PASSWORD,
        "--display-name",
        "Bob",
    )
    _run(capsys, out, "pull", str(ws_b))  # B resolves A's `general` from sync

    # --- Interleaved bidirectional sends across multiple push rounds ----------
    # Both non-guest members author into the SAME `general` stream; the server
    # assigns one gapless per-stream sequence across the two authors. Vary the
    # push order per round to vary server-arrival interleaving.
    for r in range(ROUNDS):
        for i in range(K):
            _run(capsys, out, "send", str(ws_a), "--stream", "general", "--text", f"a{r}{i}")
            _run(capsys, out, "send", str(ws_b), "--stream", "general", "--text", f"b{r}{i}")
        if r % 2 == 0:
            _run(capsys, out, "push", str(ws_a))
            _run(capsys, out, "push", str(ws_b))
        else:
            _run(capsys, out, "push", str(ws_b))
            _run(capsys, out, "push", str(ws_a))
        _run(capsys, out, "pull", str(ws_a))
        _run(capsys, out, "pull", str(ws_b))

    # --- Drive to a fixpoint: the final pull must be a no-op ------------------
    before_a = _stream_line_counts(ws_a)
    before_b = _stream_line_counts(ws_b)
    _run(capsys, out, "pull", str(ws_a))
    _run(capsys, out, "pull", str(ws_b))
    assert _stream_line_counts(ws_a) == before_a, "final pull on A appended lines — not quiescent"
    assert _stream_line_counts(ws_b) == before_b, "final pull on B appended lines — not quiescent"

    # === A1 — byte-equal stream logs A vs B ==================================
    a_streams = {p.name for p in (ws_a / "streams").iterdir() if p.is_dir()}
    b_streams = {p.name for p in (ws_b / "streams").iterdir() if p.is_dir()}
    assert a_streams == b_streams, "both devices must have the same stream ids"
    assert len(a_streams) == 2, f"expected meta + general, got {a_streams}"
    for sid in a_streams:
        a_lines = _log_lines(ws_a / "streams" / sid)
        b_lines = _log_lines(ws_b / "streams" / sid)
        assert a_lines == b_lines, f"stream {sid} logs diverge between A and B"

    # === A2 — byte-equal project dumps; full expected message count present ==
    dump_a = _project_dump(ws_a)
    dump_b = _project_dump(ws_b)
    assert dump_a == dump_b
    expected_messages = 1 + 2 * ROUNDS * K  # the a-seed + interleaved rounds
    assert _nonempty_lines(dump_a) == expected_messages, (
        f"expected {expected_messages} materialized messages, got {_nonempty_lines(dump_a)}"
    )

    # === A3 — verify green on both ==========================================
    assert main(["verify", str(ws_a)]) == 0
    capsys.readouterr()
    assert main(["verify", str(ws_b)]) == 0
    capsys.readouterr()

    # === A4 — rebuild ≡ incremental on a real client-materialized workspace ==
    rebuild_projection(Workspace.open(ws_b))
    conn = open_db(ws_b / PROJECTION_DB_NAME)
    try:
        rebuilt_dump = dump_messages(conn)
    finally:
        conn.close()
    assert rebuilt_dump == _project_dump(ws_b)

    # === A5 — idempotent replay: re-push an accepted event_id → no duplicate ==
    ws_a_open = Workspace.open(ws_a)
    replay_body = build_message_created_body(
        workspace_id=ws_a_open.workspace_id,
        stream_id=a_seed["stream_id"],
        author_user_id=ws_a_open.local_author.user_id,
        author_device_id=ws_a_open.local_author.device_id,
        client_created_at="2026-07-04T00:00:00.000Z",
        text="a-seed-replay",
        event_id=a_seed["event_id"],  # SAME event_id → server must dedupe
    ).model_dump(mode="json")
    general_before = _log_lines(ws_a / "streams" / a_seed["stream_id"])
    outbox.enqueue(ws_a_open, replay_body, hash_event(replay_body))
    _run(capsys, out, "push", str(ws_a))
    _run(capsys, out, "pull", str(ws_a))
    general_after = _log_lines(ws_a / "streams" / a_seed["stream_id"])
    assert general_after == general_before, "idempotent replay must not duplicate the event"
    assert main(["verify", str(ws_a)]) == 0
    capsys.readouterr()

    # === A6 — token hygiene: 0o600 creds; raw token in NEITHER CLI nor server log
    creds_path = credentials.msgctl_dir(ws_a_open) / credentials.CREDENTIALS_NAME
    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600
    raw_token = json.loads(creds_path.read_text())["token"]
    assert raw_token  # sanity: a token was stored
    combined_cli_output = "".join(out)
    assert raw_token not in combined_cli_output, "raw token leaked to CLI stdout/stderr"
    server_log_text = server_log.read_text(encoding="utf-8", errors="replace")
    assert raw_token not in server_log_text, "raw token leaked into server logs"
