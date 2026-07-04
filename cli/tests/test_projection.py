"""Incremental SQLite message projection (ENG-58).

Covers the ticket's ACs and stated test plan: creation, incremental idempotency,
unknown-type / above-max-version skip-with-cursor-advance (D9), version-bump
auto-rebuild (TDD §2.3 rule 5), crash-mid-apply convergence (per-stream
atomicity + ``OR IGNORE``), month-boundary contiguity, the read-only-log
guarantee (byte-compare before/after), and terminated-corruption hard errors.

Sends go through the real ``msgctl`` subprocess; projection is driven in-process
(``project(ws, conn)``) where a DB handle is needed for assertions. Synthetic
log lines (unknown type, above-max version, a second month) are hand-crafted via
``core`` exactly like ``test_scan_integrity`` so they round-trip and verify.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import msgctl.projection as projection
import pytest
from conftest import only_stream_dir, read_lines, run_cli
from msgctl.projection import (
    PROJECTION_DB_NAME,
    PROJECTION_VERSION,
    dump_messages,
    open_db,
    project,
)
from msgctl.workspace import Workspace
from msgd.core import ids
from msgd.core.envelope import Envelope, ServerMetadata
from msgd.core.hashing import hash_event
from msgd.core.payloads import build_message_created_body

# --- helpers ----------------------------------------------------------------


def _init(root: Path) -> None:
    assert run_cli("init", str(root)).returncode == 0


def _send(root: Path, stream: str, text: str, **flags: str) -> dict[str, Any]:
    """Send one message via the real CLI; return the stored envelope dict."""
    args = ["send", str(root), "--stream", stream, "--text", text]
    for key, value in flags.items():
        args += [f"--{key.replace('_', '-')}", value]
    proc = run_cli(*args)
    assert proc.returncode == 0, proc.stderr
    envelope: dict[str, Any] = json.loads(proc.stdout)
    return envelope


def _run_project(root: Path) -> tuple[projection.ProjectResult, str]:
    """Open + project + dump in-process (fresh connection each call)."""
    ws = Workspace.open(root)
    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        result = project(ws, conn)
        return result, dump_messages(conn)
    finally:
        conn.close()


def _cursor(root: Path, stream_id: str) -> int | None:
    conn = sqlite3.connect(root / PROJECTION_DB_NAME)
    try:
        row = conn.execute(
            "SELECT last_applied_seq FROM stream_cursors WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _count(root: Path) -> int:
    conn = sqlite3.connect(root / PROJECTION_DB_NAME)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        conn.close()


def _base_of(root: Path) -> dict[str, Any]:
    """The first stored line's ``body`` — a template for crafting sibling events."""
    first: dict[str, Any] = json.loads(read_lines(only_stream_dir(root))[0])
    body: dict[str, Any] = first["body"]
    return body


def _craft_created_line(
    base: dict[str, Any], *, seq: int, server_received_at: str, text: str
) -> str:
    """A valid ``message.created`` v1 line for the same stream (via core)."""
    body = build_message_created_body(
        workspace_id=base["workspace_id"],
        stream_id=base["stream_id"],
        author_user_id=base["author_user_id"],
        author_device_id=base["author_device_id"],
        client_created_at="2026-07-04T12:00:00.000Z",
        text=text,
    )
    env = Envelope(
        body=body,
        event_hash=hash_event(body.model_dump(mode="json")),
        signature=None,
        server=ServerMetadata(
            server_sequence=seq,
            server_received_at=server_received_at,
            payload_redacted=False,
        ),
    )
    return json.dumps(env.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _craft_unprojectable_line(
    base: dict[str, Any],
    *,
    seq: int,
    server_received_at: str,
    type_: str,
    type_version: int,
    payload: dict[str, Any],
) -> str:
    """A terminated but non-projectable event (unknown type or above-max version).

    Built as a raw dict + ``hash_event`` (the ``test_scan_integrity`` pattern) so
    it is a real, hash-verifiable accepted event the projection must skip — not
    crash — while still advancing the cursor (D9).
    """
    body = {
        "event_id": ids.new_event_id(),
        "workspace_id": base["workspace_id"],
        "stream_id": base["stream_id"],
        "type": type_,
        "type_version": type_version,
        "author_user_id": base["author_user_id"],
        "author_device_id": base["author_device_id"],
        "client_created_at": "2026-07-04T12:00:00.000Z",
        "payload": payload,
    }
    envelope = {
        "body": body,
        "event_hash": hash_event(body),
        "signature": None,
        "server": {
            "server_sequence": seq,
            "server_received_at": server_received_at,
            "payload_redacted": False,
        },
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


# --- tests ------------------------------------------------------------------


def test_project_creates_messages(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root)
    sent = [_send(root, "general", f"hello {i}") for i in range(3)]

    proc = run_cli("project", str(root))
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["applied"] == 3
    assert summary["skipped"] == 0

    # DB is a top-level sibling of workspace.json, never under streams/.
    assert (root / PROJECTION_DB_NAME).is_file()
    assert not (root / "streams" / PROJECTION_DB_NAME).exists()

    conn = sqlite3.connect(root / PROJECTION_DB_NAME)
    try:
        rows = conn.execute(
            "SELECT message_id, text, stream_id, server_sequence, author_user_id, format "
            "FROM messages ORDER BY server_sequence"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 3
    for i, (row, env) in enumerate(zip(rows, sent, strict=True), start=1):
        body = env["body"]
        assert row[0] == body["payload"]["message_id"]
        assert row[1] == body["payload"]["text"]
        assert row[2] == body["stream_id"]
        assert row[3] == i  # gapless server_sequence
        assert row[4] == body["author_user_id"]
        assert row[5] == body["payload"]["format"]


def test_incremental_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root)
    _send(root, "general", "a")
    _send(root, "general", "b")
    stream_id = _base_of(root)["stream_id"]

    result1, dump1 = _run_project(root)
    assert result1.applied == 2
    assert _count(root) == 2
    assert _cursor(root, stream_id) == 2

    # A second project with no new events is a true no-op.
    result2, dump2 = _run_project(root)
    assert result2.applied == 0
    assert result2.skipped == 0
    assert dump2 == dump1
    assert _count(root) == 2
    assert _cursor(root, stream_id) == 2

    # One more send → exactly one new row, cursor advanced by one.
    _send(root, "general", "c")
    result3, dump3 = _run_project(root)
    assert result3.applied == 1
    assert _count(root) == 3
    assert _cursor(root, stream_id) == 3
    assert dump3 != dump2


@pytest.mark.parametrize(
    ("type_", "type_version", "payload"),
    [
        pytest.param("widget.exploded", 7, {"blast_radius": 3}, id="unknown-type"),
        pytest.param(
            "message.created",
            2,
            {"message_id": ids.new_message_id(), "text": "from the future"},
            id="above-max-version",
        ),
    ],
)
def test_unknown_type_skips_and_advances(
    tmp_path: Path, type_: str, type_version: int, payload: dict[str, Any]
) -> None:
    root = tmp_path / "ws"
    _init(root)
    first = _send(root, "general", "real one")  # seq 1
    base = first["body"]
    recv_at = first["server"]["server_received_at"]

    # Hand-write a non-projectable event at seq 2, into the same month file.
    unknown_line = _craft_unprojectable_line(
        base,
        seq=2,
        server_received_at=recv_at,
        type_=type_,
        type_version=type_version,
        payload=payload,
    )
    month_file = next(only_stream_dir(root).glob("*.ndjson"))
    with open(month_file, "ab") as fh:
        fh.write((unknown_line + "\n").encode("utf-8"))

    _send(root, "general", "real three")  # seq 3 (unknown counted by the scan)

    log_before = month_file.read_bytes()
    result, _ = _run_project(root)

    assert result.applied == 2
    assert result.skipped == 1
    stream_id = base["stream_id"]
    assert _cursor(root, stream_id) == 3  # advanced past the skipped event

    conn = sqlite3.connect(root / PROJECTION_DB_NAME)
    try:
        message_ids = {r[0] for r in conn.execute("SELECT message_id FROM messages")}
        texts = {r[0] for r in conn.execute("SELECT text FROM messages")}
    finally:
        conn.close()
    assert texts == {"real one", "real three"}
    # The non-projectable event's payload id (if any) never became a row.
    if "message_id" in payload:
        assert payload["message_id"] not in message_ids

    # The projection never touched the log: the unknown line is byte-identical.
    assert month_file.read_bytes() == log_before
    assert read_lines(only_stream_dir(root))[1] == unknown_line


def test_version_bump_auto_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root)
    for i in range(3):
        _send(root, "general", f"m{i}")

    # A from-scratch projection of the same log = the expected post-rebuild dump.
    ws = Workspace.open(root)
    scratch = open_db(tmp_path / "scratch.sqlite3")
    try:
        project(ws, scratch)
        expected = dump_messages(scratch)
    finally:
        scratch.close()

    # Build the real projection, then plant a sentinel row a rebuild MUST drop
    # and stamp a stale version to trip the mismatch path.
    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        project(ws, conn)
        with conn:
            conn.execute(
                "INSERT INTO messages (message_id, stream_id, server_sequence, "
                "author_user_id, text, format, thread_root_id, client_created_at, "
                "server_received_at) VALUES "
                "('m_sentinel', 's_x', 999, 'u_x', 'stale', 'plain', NULL, "
                "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
            )
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'projection_version'")
    finally:
        conn.close()

    # Reopen → version mismatch → auto-rebuild (drops the sentinel) → replay.
    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        project(Workspace.open(root), conn)
        rebuilt = dump_messages(conn)
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'projection_version'"
        ).fetchone()[0]
        sentinel = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = 'm_sentinel'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert rebuilt == expected  # rebuild == incremental, sentinel gone
    assert sentinel == 0
    assert int(version) == PROJECTION_VERSION


def test_crash_mid_apply_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    _init(root)
    alpha = _send(root, "alpha", "a1")["body"]["stream_id"]
    beta = _send(root, "beta", "b1")["body"]["stream_id"]
    # project visits streams in sorted stream_id order; fail the LAST-visited one
    # so the first has already committed when the crash strikes.
    first_visited, last_visited = sorted([alpha, beta])

    original = projection._apply_message_created

    def failing(conn: sqlite3.Connection, env: Envelope) -> None:
        if env.body.stream_id == last_visited:
            raise RuntimeError("injected crash mid-apply")
        original(conn, env)

    monkeypatch.setitem(projection._HANDLERS, ("message.created", 1), failing)

    ws = Workspace.open(root)
    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            project(ws, conn)

        # First stream committed atomically; the crashed stream rolled back whole.
        first_rows = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE stream_id = ?", (first_visited,)
        ).fetchone()[0]
        last_rows = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE stream_id = ?", (last_visited,)
        ).fetchone()[0]
        assert first_rows == 1
        assert last_rows == 0
        first_cur = conn.execute(
            "SELECT last_applied_seq FROM stream_cursors WHERE stream_id = ?", (first_visited,)
        ).fetchone()
        last_cur = conn.execute(
            "SELECT last_applied_seq FROM stream_cursors WHERE stream_id = ?", (last_visited,)
        ).fetchone()
        assert first_cur[0] == 1
        assert last_cur is None  # never advanced

        # Remove the fault and re-run on the same connection → converges.
        monkeypatch.undo()
        project(ws, conn)
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        converged = dump_messages(conn)
    finally:
        conn.close()

    # Convergent state equals a clean-run projection of the same log.
    clean = open_db(tmp_path / "clean.sqlite3")
    try:
        project(Workspace.open(root), clean)
        assert dump_messages(clean) == converged
    finally:
        clean.close()


def test_month_boundary(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root)
    first = _send(root, "general", "july")  # seq 1, in 2026-07 (or whenever)
    base = first["body"]

    # A second event in the next calendar month → a separate month file.
    august_line = _craft_created_line(
        base, seq=2, server_received_at="2026-08-15T09:00:00.000Z", text="august"
    )
    (only_stream_dir(root) / "2026-08.ndjson").write_text(august_line + "\n", encoding="utf-8")

    result, _ = _run_project(root)
    assert result.applied == 2
    stream_id = base["stream_id"]
    assert _cursor(root, stream_id) == 2  # contiguous across the boundary

    conn = sqlite3.connect(root / PROJECTION_DB_NAME)
    try:
        rows = conn.execute(
            "SELECT text, server_sequence FROM messages ORDER BY server_sequence"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("july", 1), ("august", 2)]


def _snapshot_logs(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in (root / "streams").rglob("*.ndjson")}


def test_log_read_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root)
    for i in range(3):
        _send(root, "general", f"m{i}")

    before = _snapshot_logs(root)
    _run_project(root)
    after = {p: p.read_bytes() for p in (root / "streams").rglob("*.ndjson")}
    assert after == before  # projection never writes the log

    # Torn-trailing-line variant: a partial (no-\n) write must be invisible to the
    # projection AND left in place (a later send fixes it, per ENG-57).
    month_file = next(only_stream_dir(root).glob("*.ndjson"))
    torn = b'{"body":{"event_id":"m_partial'  # crashed mid-write, no newline
    with open(month_file, "ab") as fh:
        fh.write(torn)
    with_torn = month_file.read_bytes()

    count_before = _count(root)
    result, _ = _run_project(root)
    assert result.applied == 0  # nothing new beyond the cursor; torn line ignored
    assert _count(root) == count_before
    # The torn bytes are still there, untouched (not truncated, not repaired).
    assert month_file.read_bytes() == with_torn


def test_corrupt_terminated_line_hard_errors(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _init(root)
    _send(root, "general", "good")

    month_file = next(only_stream_dir(root).glob("*.ndjson"))
    with open(month_file, "ab") as fh:
        fh.write(b"this is not json\n")  # terminated corruption
    before = month_file.read_bytes()

    proc = run_cli("project", str(root))
    assert proc.returncode == 1
    assert proc.stderr.startswith("msgctl:")
    assert "Traceback" not in proc.stderr
    # Read-only even on the error path: the log is byte-identical.
    assert month_file.read_bytes() == before
