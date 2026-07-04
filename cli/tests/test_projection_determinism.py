"""Determinism contract for the projection (ENG-58 → the ENG-61 equivalence gate).

Two invariants, exercised locally:

- **Identical logs → identical dump.** ``dump_messages`` is a pure function of the
  log contents, free of rowid / iteration-order / wall-clock-at-projection-time
  dependence. Two workspaces holding byte-identical logs must yield a
  byte-identical normalized dump.
- **Rebuild == incremental.** A projection built incrementally (send, project,
  send, project …) and one produced by a forced full rebuild of the same log
  yield a byte-identical dump — the ``rebuild ≡ incremental`` invariant ENG-61
  asserts across a whole workspace.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from conftest import run_cli
from msgctl.projection import (
    PROJECTION_DB_NAME,
    PROJECTION_VERSION,
    dump_messages,
    open_db,
    project,
)
from msgctl.workspace import Workspace


def _send(root: Path, stream: str, text: str) -> None:
    assert run_cli("send", str(root), "--stream", stream, "--text", text).returncode == 0


def _dump(root: Path) -> str:
    ws = Workspace.open(root)
    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        project(ws, conn)
        return dump_messages(conn)
    finally:
        conn.close()


def test_two_workspaces_identical_dump(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    assert run_cli("init", str(root_a)).returncode == 0
    for stream in ("general", "random"):
        for i in range(3):
            _send(root_a, stream, f"{stream} {i}")

    # Workspace B is a byte-identical copy of A's log + manifest (no projection
    # DB copied — A hasn't been projected yet). Same log ⇒ same dump.
    root_b = tmp_path / "b"
    shutil.copytree(root_a, root_b)

    dump_a = _dump(root_a)
    dump_b = _dump(root_b)

    assert dump_a != ""  # the test is meaningful only with rows present
    assert dump_a.count("\n") == 5  # 6 messages → 6 lines
    assert dump_a == dump_b


def test_rebuild_equals_incremental(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    assert run_cli("init", str(root)).returncode == 0

    # Build incrementally: interleave sends across two streams with projects.
    for i in range(3):
        _send(root, "general", f"g{i}")
        _send(root, "random", f"r{i}")
        conn = open_db(root / PROJECTION_DB_NAME)
        try:
            project(Workspace.open(root), conn)
        finally:
            conn.close()

    conn = open_db(root / PROJECTION_DB_NAME)
    try:
        incremental = dump_messages(conn)
    finally:
        conn.close()

    # Force the version-mismatch rebuild path: stamp a stale version, reopen.
    raw = sqlite3.connect(root / PROJECTION_DB_NAME)
    try:
        with raw:
            raw.execute("UPDATE meta SET value = '0' WHERE key = 'projection_version'")
    finally:
        raw.close()

    conn = open_db(root / PROJECTION_DB_NAME)  # mismatch → _rebuild_schema
    try:
        project(Workspace.open(root), conn)
        rebuilt = dump_messages(conn)
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'projection_version'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert incremental != ""
    assert incremental == rebuilt
    assert int(version) == PROJECTION_VERSION
