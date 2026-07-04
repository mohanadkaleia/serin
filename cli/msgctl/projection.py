"""Incremental SQLite message projection (ENG-58).

``msgctl project <dir>`` reads the append-only NDJSON log ENG-57 produces and
**incrementally** materializes a ``messages`` table in a SQLite DB inside the
workspace. It is the M0 stand-in for the M1 server's ``messages_proj`` (TDD §4.2)
and obeys the three permanent projection invariants:

- **Incremental & idempotent** — a per-stream cursor persisted in the DB means a
  second ``project`` with no new log events applies nothing and mutates nothing.
- **Version-gated** — :data:`PROJECTION_VERSION` is declared here and stored in
  the DB; a mismatch on :func:`open_db` forces an automatic full rebuild (TDD
  §2.3 rule 5). ENG-59's user-facing ``msgctl rebuild`` calls the same seam
  (:func:`_rebuild_schema` then :func:`project`); this ticket ships only the
  auto-on-mismatch path.
- **D9-safe & deterministic** — unknown event types (and ``message.created``
  versions above the reader's max) are skipped in the projection but their
  sequence is still consumed by the cursor; the log is never touched; and the
  final table state is a pure function of the log, so two workspaces with
  identical logs yield an **identical normalized dump** (:func:`dump_messages`,
  the artifact ENG-61 diffs).

**Durability is intentionally cheap, unlike the log.** ENG-57's ``append_event``
fsyncs before it acks because a lost *acked* event is unrecoverable. The
projection is the opposite: it is a **pure function of the log** and can always
be rebuilt, so we do **no** manual ``fsync``/dir-fsync and keep SQLite's default
``synchronous`` and default rollback journal (``journal_mode=DELETE``). A torn
projection write is not a data-loss event — the next ``project`` (or an ENG-59
``rebuild``) reconstructs it. Do not "fix" this toward the log's fsync discipline.

The DB lives at ``<dir>/projections.sqlite3`` — a top-level sibling of
``workspace.json``, deliberately **outside** ``streams/`` so it never corrupts
the §9 export shape and is excluded from export/verify walks **by path** (they
enumerate ``streams/<id>/*.ndjson`` + the two named manifests, never globbing the
root). The transient ``projections.sqlite3-journal`` exists only *during* a
transaction, so between runs the workspace holds exactly one extra file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from msgd.core.envelope import Envelope
from msgd.core.payloads import MessageCreatedV1
from pydantic import ValidationError

from msgctl.errors import CorruptLogError
from msgctl.workspace import Workspace

__all__ = [
    "PROJECTION_VERSION",
    "PROJECTION_DB_NAME",
    "ProjectResult",
    "open_db",
    "project",
    "dump_messages",
]

#: The projection contract version. It governs **both** the schema shape **and**
#: the projection logic: bump it on ANY change to either — add/drop a column, add
#: or change a handler, or change how a field maps. A bump forces a full rebuild
#: on the next :func:`open_db` (ENG-59/62: remember to bump when you touch this).
PROJECTION_VERSION: Final = 1

#: The projection DB file name, at the workspace root (sibling of
#: ``workspace.json``, outside ``streams/`` — see the module docstring).
PROJECTION_DB_NAME: Final = "projections.sqlite3"

# All CREATEs are IF NOT EXISTS so _init_schema is safe both on a fresh DB and
# after _rebuild_schema drops only the data tables (meta survives a rebuild).
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS messages (
  message_id         TEXT PRIMARY KEY,
  stream_id          TEXT NOT NULL,
  server_sequence    INTEGER NOT NULL,
  author_user_id     TEXT NOT NULL,
  text               TEXT NOT NULL,
  format             TEXT NOT NULL,
  thread_root_id     TEXT,
  client_created_at  TEXT NOT NULL,
  server_received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_stream_seq
  ON messages (stream_id, server_sequence);

CREATE TABLE IF NOT EXISTS stream_cursors (
  stream_id        TEXT PRIMARY KEY,
  last_applied_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

#: Columns of the ENG-61 normalized dump, in fixed order (never ``SELECT *``).
_DUMP_COLUMNS: Final = (
    "message_id",
    "stream_id",
    "server_sequence",
    "author_user_id",
    "text",
    "format",
    "thread_root_id",
    "client_created_at",
    "server_received_at",
)


@dataclass
class ProjectResult:
    """Outcome of a :func:`project` run.

    ``applied`` counts rows projected this run, ``skipped`` counts events read but
    not projected (unknown types / above-max versions — D9). ``stream_heads`` maps
    each stream to the highest ``server_sequence`` its cursor now reflects.
    """

    applied: int = 0
    skipped: int = 0
    stream_heads: dict[str, int] = field(default_factory=dict)


def _server_sequence(env: Envelope) -> int:
    """The event's ``server_sequence`` (``env.server`` is guaranteed non-null here).

    :func:`_read_stream_events` raises :class:`CorruptLogError` on any stored line
    missing server metadata, so every ``Envelope`` a handler or the cursor logic
    sees has a populated ``server`` — the assert just narrows the type for mypy.
    """
    assert env.server is not None
    return env.server.server_sequence


def _server_received_at(env: Envelope) -> str:
    assert env.server is not None
    return env.server.server_received_at


# --- schema / version management -------------------------------------------


def _write_version(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('projection_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(PROJECTION_VERSION),),
        )


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables + the index and stamp the current version."""
    conn.executescript(_SCHEMA)
    _write_version(conn)


def _rebuild_schema(conn: sqlite3.Connection) -> None:
    """Drop the data tables + cursors, recreate the schema, stamp the version.

    This is the **rebuild seam** ENG-59 imports unchanged: its ``msgctl rebuild``
    is ``_rebuild_schema(conn)`` then ``project(ws, conn)`` — the exact two calls
    the auto-on-mismatch path performs, so ENG-59 adds no new projection logic.
    ``meta`` is deliberately kept (only ``messages`` + ``stream_cursors`` are
    dropped); cursors are now empty, so the ``project`` that runs next replays the
    entire log — "drop tables + reset cursors + replay", TDD §2.3 rule 5.
    """
    with conn:
        conn.execute("DROP TABLE IF EXISTS messages")
        conn.execute("DROP TABLE IF EXISTS stream_cursors")
    _init_schema(conn)


def _read_version(conn: sqlite3.Connection) -> int | None:
    """Stored ``projection_version``, or ``None`` if the DB is fresh (no ``meta``)."""
    has_meta = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta is None:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'projection_version'").fetchone()
    return int(row[0]) if row is not None else None


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Open the projection DB, running the version-gate (TDD §2.3 rule 5).

    Three cases, decided here so ``cmd_project`` stays a straight-line
    "open → project → dump":

    1. **Fresh DB** (no ``meta``) → create the schema and stamp the version. Not a
       rebuild (nothing to rebuild); cursors start empty so the first ``project``
       applies everything.
    2. **Version matches** → proceed incrementally, cursors intact.
    3. **Version mismatch** → auto-rebuild via :func:`_rebuild_schema` (drop +
       recreate data tables, reset cursors, stamp the new version); the following
       ``project`` replays the whole log from empty cursors.

    Uses the default rollback journal and default ``synchronous`` — the projection
    is rebuildable, so durability is intentionally cheap (module docstring).
    """
    conn = sqlite3.connect(db_path)
    stored = _read_version(conn)
    if stored is None:
        _init_schema(conn)
    elif stored != PROJECTION_VERSION:
        _rebuild_schema(conn)
    return conn


# --- read-only log walk -----------------------------------------------------


def _read_stream_events(stream_dir: Path) -> list[Envelope]:
    """Read a stream's whole terminated history as ordered :class:`Envelope`s.

    A **read-only** walk (Ruling 7): it never truncates or repairs the log — a
    contrast with ENG-57's ``_scan_stream``, which *mutates* (truncates torn
    lines) and is shaped for append, not an ordered projection read. We therefore
    duplicate a minimal reader rather than reuse it.

    - **Torn trailing line** (bytes after the last ``\\n``, if any): a not-yet-
      durable / crashed partial write. It is simply *not yet visible* to the
      projection — dropped **without truncating** the file (a later ``send``
      fixes it, per ENG-57).
    - **Terminated-but-corrupt line** (fails ``json.loads`` or
      ``Envelope.model_validate``): corruption a well-behaved writer never emits →
      :class:`CorruptLogError`. Never silently skipped, never repaired.
    - **Contiguity**: ``server_sequence == prev + 1`` (first ``== 1``) over the
      terminated events — the D2 integrity property, cheaply re-checked. The torn
      trailing line is excluded (never acked → its absence is not a gap).

    Returns the stream's full ordered history; the caller applies only the tail
    beyond its persisted cursor.
    """
    events: list[Envelope] = []
    if not stream_dir.is_dir():
        return events

    last_seq = 0
    for path in sorted(stream_dir.glob("*.ndjson")):
        raw = path.read_bytes()
        if not raw:
            continue
        # Split on "\n"; the final element is the bytes after the last newline:
        # empty when the file ends with "\n" (fully terminated), else a torn
        # trailing line we drop WITHOUT touching the file.
        parts = raw.split(b"\n")
        for line in parts[:-1]:
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorruptLogError(f"corrupt terminated line in {path}: {exc}") from exc
            try:
                env = Envelope.model_validate(parsed)
            except ValidationError as exc:
                raise CorruptLogError(f"corrupt terminated line in {path}: {exc}") from exc
            if env.server is None:
                raise CorruptLogError(f"stored line missing server metadata in {path}")
            seq = env.server.server_sequence
            if seq != last_seq + 1:
                raise CorruptLogError(
                    f"sequence gap in {path}: expected {last_seq + 1}, found {seq}"
                )
            last_seq = seq
            events.append(env)
    return events


# --- apply / dispatch -------------------------------------------------------


def _apply_message_created(conn: sqlite3.Connection, env: Envelope) -> None:
    """Project one ``message.created`` v1 event into ``messages``.

    Validates the opaque ``payload`` through :class:`MessageCreatedV1` (a clean
    error on a malformed known payload) and ``INSERT OR IGNORE``s by
    ``message_id`` — ``message.created`` is **immutable** in M0 (edits are a future
    ``message.edited`` event), so an existing row and a re-projected one are
    byte-identical; ``IGNORE`` (keep existing) avoids needless churn and keeps the
    dump stable regardless of the cursor (Ruling 4).

    A payload that fails :class:`MessageCreatedV1` is **not** a D9 skip case (D9
    covers *unknown* types/versions): the only M0 writer validates the payload
    before writing, so an invalid known payload in the log is corruption a
    well-behaved writer never emits → :class:`CorruptLogError` (Ruling 7). The
    raise unwinds out of the per-stream ``with conn`` transaction, rolling back
    that stream's partial batch including the cursor bump.
    """
    try:
        payload = MessageCreatedV1(**env.body.payload)
    except ValidationError as exc:
        raise CorruptLogError(
            f"invalid message.created v1 payload in stream {env.body.stream_id} "
            f"seq {_server_sequence(env)} (event {env.body.event_id}): {exc}"
        ) from exc
    conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(message_id, stream_id, server_sequence, author_user_id, text, format, "
        "thread_root_id, client_created_at, server_received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payload.message_id,
            env.body.stream_id,
            _server_sequence(env),
            env.body.author_user_id,
            payload.text,
            payload.format,
            payload.thread_root_id,
            env.body.client_created_at,
            _server_received_at(env),
        ),
    )


#: The projection's own dispatch, keyed on ``(type, type_version)`` — distinct
#: from ``core``'s payload-validation registry. In M0 exactly one handler exists;
#: everything else (unknown types, ``message.created`` v>=2, future known-but-
#: unhandled types) has no handler and is uniformly skipped-with-cursor-advance.
_HANDLERS: Final[dict[tuple[str, int], Callable[[sqlite3.Connection, Envelope], None]]] = {
    ("message.created", 1): _apply_message_created,
}


def project(ws: Workspace, conn: sqlite3.Connection) -> ProjectResult:
    """Incrementally apply the log to the projection DB.

    Streams are visited in lexicographic ``stream_id`` order and, within a stream,
    events in ascending ``server_sequence`` (Ruling 5). Ordering does not affect
    the final state — per-stream cursors make cross-stream interleaving
    irrelevant and ``INSERT OR IGNORE`` on the immutable ``message_id`` makes
    within-stream re-apply idempotent — but it is fixed so a run is reproducible
    and free of iteration-order/wall-clock dependence.

    For each stream, all of its new-event row upserts **and** its single cursor
    bump run in **one** ``with conn`` transaction (Ruling 4): a crash between an
    insert and the cursor update rolls both back, so a stream is never left
    half-applied. The cursor advances to ``max(server_sequence)`` over **all** new
    events read — projected or not — so a skipped unknown event is never re-read.
    """
    result = ProjectResult()
    for stream_id in sorted(ws.streams):
        row = conn.execute(
            "SELECT last_applied_seq FROM stream_cursors WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        last_applied = row[0] if row is not None else 0

        events = _read_stream_events(ws.stream_dir(stream_id))
        new = [e for e in events if _server_sequence(e) > last_applied]
        if not new:
            if events:
                result.stream_heads[stream_id] = _server_sequence(events[-1])
            continue

        # One atomic transaction for this stream: row upserts + the cursor bump
        # commit together or not at all (Ruling 4).
        with conn:
            for env in new:
                handler = _HANDLERS.get((env.body.type, env.body.type_version))
                if handler is not None:
                    handler(conn, env)
                    result.applied += 1
                else:
                    # Unknown type or above-max version (D9): skip the row but the
                    # event still occupied a sequence, so the cursor advances past
                    # it below. Never crash.
                    result.skipped += 1
            new_head = _server_sequence(new[-1])  # events are ascending
            conn.execute(
                "INSERT INTO stream_cursors (stream_id, last_applied_seq) VALUES (?, ?) "
                "ON CONFLICT(stream_id) DO UPDATE SET last_applied_seq = excluded.last_applied_seq",
                (stream_id, new_head),
            )
        result.stream_heads[stream_id] = new_head
    return result


def dump_messages(conn: sqlite3.Connection) -> str:
    """The ENG-61 contract surface: a normalized, deterministic ``messages`` dump.

    A fixed explicit-column ``SELECT ... ORDER BY stream_id, server_sequence``
    (never ``SELECT *``, never the implicit rowid, no wall-clock), each row
    serialized to one compact JSON object with a fixed key order and
    ``ensure_ascii=False``, ``\\n``-joined. SQLite *file bytes* are not
    deterministic (page layout, freelist, rowid allocation), so ENG-61 compares
    **this** text, not the raw DB. Two workspaces with identical logs → a
    byte-identical dump; a rebuilt projection and an incrementally-built one →
    a byte-identical dump (rebuild ≡ incremental).

    ENG-61 may import this function or reimplement the identical query + this
    exact serialization (compact separators, fixed ``_DUMP_COLUMNS`` key order,
    ``ensure_ascii=False``).
    """
    rows = conn.execute(
        "SELECT message_id, stream_id, server_sequence, author_user_id, text, "
        "format, thread_root_id, client_created_at, server_received_at "
        "FROM messages ORDER BY stream_id, server_sequence"
    ).fetchall()
    return "\n".join(
        json.dumps(
            dict(zip(_DUMP_COLUMNS, row, strict=True)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in rows
    )
