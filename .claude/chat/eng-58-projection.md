# ENG-58 — M0: Incremental SQLite message projection with `PROJECTION_VERSION`

**Milestone:** M0 — Protocol spike
**Tech-lead:** planning complete; all implementation is **`python-engineer`**.
**TDD refs:** §2.3 (schema-evolution contract D9 — rules 3 & 5 are the load-bearing ones here),
§4.2 (`messages_proj` server projection — the column model we mirror as an M0 SQLite subset), §4.3
(the accept→apply→rebuild contract), §2.1/§2.2 (envelope + `message.created` v1 payload), §9 (the
`streams/<stream_id>/<YYYY-MM>.ndjson` export tree the log already is). Locked decisions: **D2**
(gapless per-stream `server_sequence`), **D9** (unknown types/versions preserved-not-crashed), **D14**
(`client_created_at` untrusted). Permanent CI invariant this ticket seeds: **rebuild ≡ incremental**
(§2.3 rule 5, §4.2, M0 exit) — the equivalence gate itself is ENG-61.
**Depends on (all merged to main):** ENG-53/54/55/56 (`core/`), **ENG-57** (`msgctl init`/`send`, the
NDJSON workspace + scan-on-open semantics — see `.claude/chat/eng-57-msgctl-append.md`).
**Runs in parallel with ENG-60 (`msgctl verify`)** — ownership partition is §6, and it is the first
thing the implementer should read.

---

## 1. Goal (restated)

`msgctl project <dir>` reads the append-only log ENG-57 produces and **incrementally** materializes a
`messages` table in a SQLite DB inside the workspace. It is the M0 stand-in for the server's
`messages_proj` (§4.2) and it must obey the three permanent projection invariants:

- **Incremental & idempotent** — a per-stream cursor persisted in the DB means a second `project`
  with no new log events applies nothing and mutates nothing.
- **Version-gated** — a `PROJECTION_VERSION` declared in code and stored in the DB; a mismatch on
  open forces an automatic full rebuild (§2.3 rule 5). This ticket implements the *internal*
  rebuild-on-mismatch; the user-facing `msgctl rebuild` is **ENG-59** and will call the same seam.
- **D9-safe & deterministic** — unknown event types (and `message.created` versions above the
  reader's max) are skipped in the projection but their sequence is still consumed by the cursor;
  the log is never touched; and the final table state is a pure function of the log, so two
  workspaces with identical logs yield an **identical normalized dump** (the artifact ENG-61 diffs).

Areas touched: **new** `cli/msgctl/projection.py`, a surgical append to `cli/msgctl/cli.py`, new
`cli/tests/test_projection*.py`. **No `core/` edits, no new runtime dependency** (`sqlite3` is
stdlib), **no changes to `append.py`/`workspace.py`/`errors.py`** (read-only reuse only — §6).

---

## 2. Design rulings (each ticket question, ruled)

### Ruling 1 — DB location & name → `<dir>/projections.sqlite3`, single file, outside `streams/`

- **Path:** `PROJECTION_DB_NAME = "projections.sqlite3"` at the **workspace root**, a sibling of
  `workspace.json` — deliberately **not** under `streams/`. The `streams/` subtree is byte-for-byte
  the §9 export shape (ENG-57 Ruling 1); a projection file inside it would corrupt that shape and
  leak a derived artifact into every future export/verify walk.
- **Excluded from export/verify by construction.** §9's export copies `streams/` and synthesizes
  `manifest.json`; a `verify` walk (ENG-60) recomputes hashes over `streams/**/*.ndjson` and reads
  `workspace.json`. Because `projections.sqlite3` is a top-level file matching neither the
  `streams/` descent nor the two named manifests, it is outside both walks **by path** — no
  ignore-list needed. Record this as the contract ENG-60 relies on: *verify enumerates
  `streams/<id>/*.ndjson` and the two manifests explicitly; it never globs the workspace root*, so
  the projection DB (and its journal) can never enter a hash/contiguity check.
- **Single-file discipline — rollback journal, not WAL.** Open with the default `journal_mode`
  (`DELETE`): the transient `projections.sqlite3-journal` exists only *during* a transaction and is
  removed at commit, so between runs the workspace holds exactly **one** extra file. WAL is
  rejected for M0 precisely because its persistent `-wal`/`-shm` sidecars would multiply the
  root-level derived files and muddy the "one clean DB, excluded by path" story. (If a future
  ticket wants WAL for concurrency, it must also teach verify/export to skip all three suffixes —
  noted, not now.)
- **Durability is intentionally cheap.** Unlike the log (ENG-57 fsyncs before ack because a lost
  acked event is unrecoverable), the projection is **disposable** — it is a pure function of the
  log and can always be rebuilt. So we do **no** manual `fsync`/dir-fsync here and keep SQLite's
  default `synchronous`. A torn projection write is not a data-loss event; the next `project` (or an
  ENG-59 `rebuild`) reconstructs it. State this contrast explicitly in the module docstring so a
  reviewer doesn't "fix" it toward the log's durability discipline.

### Ruling 2 — Schema (`messages`, `meta`, `stream_cursors`; no FTS in M0)

Mirror §4.2 `messages_proj` as the M0 SQLite subset — the columns the ticket lists, no server-only
denormalizations (`reply_count`, `edited_seq`, `deleted`, `search_tsv` are for the edit/reaction/FTS
tickets, not M0 where only `message.created` v1 exists):

```sql
CREATE TABLE messages (
  message_id        TEXT PRIMARY KEY,           -- idempotency anchor (Ruling 4)
  stream_id         TEXT NOT NULL,
  server_sequence   INTEGER NOT NULL,           -- the created_seq (§4.2)
  author_user_id    TEXT NOT NULL,
  text              TEXT NOT NULL,
  format            TEXT NOT NULL,
  thread_root_id    TEXT,                        -- nullable (root messages)
  client_created_at TEXT NOT NULL,               -- untrusted (D14), stored not ordered-on
  server_received_at TEXT NOT NULL
);
CREATE INDEX idx_messages_stream_seq ON messages (stream_id, server_sequence);

CREATE TABLE stream_cursors (
  stream_id        TEXT PRIMARY KEY,
  last_applied_seq INTEGER NOT NULL              -- highest server_sequence applied for this stream
);

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                            -- holds key='projection_version'
);
```

- **`message_id` is the PK** — this is what makes re-apply idempotent independent of the cursor
  (Ruling 4). The `(stream_id, server_sequence)` index exists **only** to make the ENG-61 normalized
  dump's `ORDER BY` cheap and to keep the dump off the implicit rowid (Ruling 5).
- **Cursors get their own table**, not KV rows in `meta` — one row per stream, typed INTEGER,
  updated atomically with that stream's inserts (Ruling 4). `meta` holds only the scalar
  `projection_version` in M0; keeping the two concerns in separate tables keeps the rebuild reset
  trivial (drop `messages`+`stream_cursors`, keep `meta`).
- **`message.created` v>1 (future) → skipped as above-max version (D9).** The dispatch (Ruling 6)
  keys on `(type, type_version)`; `("message.created", 2)` has no handler in M0, so it skips with
  cursor-advance exactly like an unknown type. When a v2 handler ships it also **bumps
  `PROJECTION_VERSION`** (the schema/logic changed), forcing a rebuild that re-projects the v2 rows.
- **No FTS in M0.** `search_tsv` + its GIN index (§4.2) are Postgres-only anyway; the M0 SQLite
  projection is for equivalence-gate and `messages` correctness, not search. FTS5 is a later ticket.

### Ruling 3 — `PROJECTION_VERSION`: declared in code, stored in `meta`, checked on open, auto-rebuild on mismatch

- **Declared:** `PROJECTION_VERSION: Final = 1` at the top of `projection.py`. It governs **both** the
  schema shape **and** the projection logic — any change to either (add a column, add/change a
  handler, change how a field maps) bumps it. A one-line comment says so, so ENG-59/62 remember.
- **Stored:** `meta(key='projection_version', value=str(PROJECTION_VERSION))`.
- **Checked on open (`open_db`)**, three cases:
  1. **Fresh DB** (file/`meta` row absent) → create schema, write current version. *Not* a rebuild
     (nothing to rebuild); cursors simply start empty so the first `project` applies everything.
  2. **Version matches** → proceed incrementally, cursors intact.
  3. **Version mismatch** (stored ≠ code) → **auto-rebuild**: call the internal `_rebuild_schema`
     seam (drop+recreate `messages`+`stream_cursors`, write current version). Cursors are now empty,
     so the `project()` that runs immediately after in `cmd_project` **replays the entire log** →
     "drop tables + reset cursors + replay", exactly §2.3 rule 5.
- **Rebuild seam for ENG-59 (design it, ship only the auto path).** Structure the module so rebuild
  is one internal function ENG-59's `msgctl rebuild` can import and call unchanged:

  ```python
  def _rebuild_schema(conn) -> None:      # drop data tables + cursors, recreate, set version
      ...
  # auto path (this ticket): open_db() calls _rebuild_schema on version mismatch.
  # ENG-59 rebuild command = _rebuild_schema(conn) then project(ws, conn) — same two calls
  #   the auto path already performs; no new projection logic in ENG-59.
  ```

  Because `rebuild == reset-then-project` and `project` already replays from empty cursors, ENG-59
  is a thin CLI subcommand over this seam. Ship **only** the auto-on-mismatch trigger now; do not add
  a `rebuild` subcommand (that is ENG-59's cli.py append).

### Ruling 4 — Transaction discipline & idempotency → per-stream atomic batch + `INSERT OR IGNORE` by `message_id`

Rule the **combination** (both belt and suspenders, each covering a distinct failure):

- **Per-stream atomic transaction.** For each stream, wrap *all* of that stream's new-event row
  upserts **and** the single `stream_cursors` bump in **one** transaction (`with conn:` — commits on
  success, rolls back on exception). A crash between a row insert and the cursor update therefore
  cannot leave a half-applied stream: either the rows *and* the advanced cursor commit together, or
  neither does. On rerun the cursor reflects exactly what committed, so no double-apply and no skip.
- **`INSERT OR IGNORE INTO messages` keyed by `message_id`.** This makes re-projecting an
  already-present message a no-op **regardless of the cursor** — the idempotency safety net if a
  cursor is ever stale or a batch is re-read. `OR IGNORE` (keep existing) over `OR REPLACE`
  (overwrite) because `message.created` is **immutable** in M0 (edits are a future `message.edited`
  event, not a re-`message.created`), so an existing row and a re-projected one are byte-identical —
  `IGNORE` avoids needless row churn and keeps the existing row (and thus the dump) perfectly stable.
- **Why both, not just one:** the transaction guarantees cursor/rows atomicity (correct incremental
  advance); `OR IGNORE` guarantees a re-read never duplicates even if the cursor logic is bypassed
  (e.g. a rebuild replay over a not-fully-empty table, or a future concurrent projector). Neither
  alone is as robust: a transaction without `OR IGNORE` would double-insert on a replay that doesn't
  reset cursors; `OR IGNORE` without the atomic cursor could advance the cursor past rows that
  rolled back. Ruled: keep both.
- **Batch granularity:** one transaction **per stream per `project` run** (not per event). At M0
  scale (local, human send rate) this is trivially cheap and gives clean per-stream atomicity. Note
  as a perf lever only: if a single stream ever has a huge backlog, chunk into N-event
  sub-transactions behind the same cursor discipline — deferred, no interface change.

### Ruling 5 — Determinism contract (the ENG-61 equivalence gate)

State the invariants precisely and pin what ENG-61 compares:

1. **Final DB *state* is a pure function of the log contents.** The set of `messages` rows and
   `stream_cursors` values depends only on the events in the log — **not** on the order streams are
   visited, nor on wall-clock, nor on dict/glob iteration order. This holds because per-stream
   cursors make cross-stream interleaving irrelevant and `INSERT OR IGNORE` on the immutable
   `message_id` makes within-stream re-apply idempotent.
2. **Fixed apply order anyway:** streams visited in **lexicographic `stream_id`** order; within a
   stream, events applied in **ascending `server_sequence`**. Ordering does not affect final state
   (invariant 1) but is fixed so a run is reproducible and free of iteration-order/wall-clock
   dependence. `sorted()` on the stream ids and on the month-file glob; never rely on `dict`
   insertion order or SQLite rowid.
3. **ENG-61 compares a normalized SELECT dump, NOT raw DB bytes.** SQLite file bytes are *not*
   deterministic (page layout, freelist, rowid allocation, internal timestamps), so a byte diff of
   `projections.sqlite3` is meaningless. The canonical artifact is:

   ```sql
   SELECT message_id, stream_id, server_sequence, author_user_id, text,
          format, thread_root_id, client_created_at, server_received_at
   FROM messages
   ORDER BY stream_id, server_sequence;
   ```

   serialized to a stable text form (one compact JSON object per row, `ensure_ascii=False`,
   fixed key order, `\n`-joined). Explicit column list (**never `SELECT *`**), explicit `ORDER BY`,
   no rowid, no wall-clock. Two workspaces with identical logs → **byte-identical dump**; a rebuilt
   projection and an incrementally-built one → **byte-identical dump** (this is the rebuild≡incremental
   invariant ENG-61 asserts across a whole workspace).
4. **Ship the serializer here:** `dump_messages(conn) -> str` in `projection.py` is the single
   authoritative dump function. ENG-61 imports it (or reimplements the identical query) — do not let
   the equivalence gate invent its own ordering. Record `dump_messages` as the ENG-61 contract
   surface now.

### Ruling 6 — Unknown / known-but-unhandled types: skip in projection, **advance the cursor**

- The projection has its **own handler dispatch**, distinct from `core`'s payload *validation*
  registry (`get_payload_model`): `core` says "is this payload schema known?", the projection says
  "do I know how to *project* this?". In M0 exactly one handler exists:

  ```python
  _HANDLERS = { ("message.created", 1): _apply_message_created }
  ```

- For each event read from the log, in ascending sequence:
  - `handler = _HANDLERS.get((body.type, body.type_version))`.
  - **Handler found** → validate the payload via `MessageCreatedV1` (or read the opaque `payload`
    dict fields directly) and `INSERT OR IGNORE` the row.
  - **No handler** → **skip** (insert nothing) but the event still **occupied a sequence**, so the
    cursor advances past it. This one branch covers *everything* non-projectable in M0: unknown
    types (`widget.exploded`), `message.created` v≥2 (above-max version, D9), and known-but-unhandled
    future types (`message.edited`, `reaction.added`) that don't exist yet. Ruled uniformly:
    **only `message.created` v1 projects; all else skips-with-cursor-advance.**
- The cursor is therefore `= max(server_sequence)` over **all** events read for the stream, projected
  or not — never `max` over only the projected ones (that would re-read/re-skip the same unknown
  events forever and, worse, could re-apply a later message after a projected-then-skipped gap).
- Never crash on an unknown type (D9). The event stays in the log untouched (Ruling 7).
- **Test hook:** a synthetic terminated `widget.exploded` / `type_version=7` event between two real
  `message.created`s (the exact pattern already in `cli/tests/test_scan_integrity.py`) — assert it is
  absent from `messages`, both real messages present, and the cursor advanced past all three.

### Ruling 7 — Torn / corrupt log during projection read → **read-only, never repair**

Reuse ENG-57's *semantics*, but **not** its `_scan_stream`/`_scan_file` code, and never mutate the
log:

- **Torn trailing line:** a month file's bytes after the last `\n` (if non-empty) are a not-yet-durable
  / crashed partial write. Projection **ignores** it — it is simply *not yet visible* to the
  projection — and does **not** truncate, warn-and-modify, or otherwise touch the file. (Contrast
  ENG-57's `append_event`, which *does* truncate torn lines because it is about to append; projection
  is a pure reader and must leave the byte the next `send` will fix.)
- **Terminated-but-corrupt line** (fails `json.loads`, or fails `Envelope.model_validate`): this is
  corruption a well-behaved writer never emits → raise `CorruptLogError` (exit 1, clean `msgctl:`
  stderr via the existing `main` handler). Do **not** silently skip (would mask data loss) and do
  **not** repair. Same hard-error contract as ENG-57's scan.
- **Contiguity check:** the read-only scan verifies per-stream `seq == prev + 1` (first `== 1`) over
  the terminated events it reads, raising `CorruptLogError` on a gap — the D2 integrity property the
  log guarantees, cheaply re-checked. (A torn trailing line is excluded from the run, which is
  correct: it was never acked, so its absence is not a gap.)
- **Why duplicate ~30 lines instead of importing `_scan_stream`:** ENG-57's `_scan_stream` (a)
  *truncates* torn lines (a mutation — violates projection's read-only guarantee) and (b) returns
  only `(last_seq, event_id→line)`, not the ordered parsed `Envelope`s with payloads a projection
  needs. It cannot be reused **as-is** for a read-only ordered walk. Per the coordination brief's
  allowance, `projection.py` gets its own minimal read-only reader,
  `_read_stream_events(stream_dir) -> list[Envelope]` (glob `*.ndjson` sorted, split on `\n`, drop the
  non-terminated trailing element **without truncating**, parse each terminated line → `Envelope` or
  `CorruptLogError`, check contiguity). We still import ENG-57/core's **public, unmodified** pieces
  (`Envelope`, `CorruptLogError`, `Workspace`, constants) — no refactor of any shared file.
- **Perf note:** each `project` run re-reads every month file (O(total events)) even though it only
  *applies* the tail beyond the cursor (O(new events)). The **DB writes stay incremental**; only the
  read is full-scan. Acceptable at M0 (same O(n)-open budget ENG-57 already spends). A future
  optimization persists a per-stream byte offset with the cursor — deferred, noted.

---

## 3. File list

**Create:**

| File | Purpose |
|---|---|
| `cli/msgctl/projection.py` | Everything: `PROJECTION_VERSION`, `PROJECTION_DB_NAME`, schema DDL, `open_db` (connect + version check + auto-rebuild), `_init_schema`, `_rebuild_schema` (the ENG-59 seam), `_read_stream_events` (read-only log walk), `_HANDLERS` + `_apply_message_created`, `project(ws, conn) -> ProjectResult`, `dump_messages(conn) -> str` (the ENG-61 contract surface). |
| `cli/tests/test_projection.py` | Idempotency, unknown-type skip+advance, above-max version skip, version-bump auto-rebuild, crash-mid-apply convergence, month-boundary, read-only guarantee. |
| `cli/tests/test_projection_determinism.py` | Two workspaces / identical logs → identical `dump_messages`; rebuild-dump == incremental-dump (the ENG-61 invariant, exercised locally). |

**Modify (surgical, append-only — §6):**

| File | Change |
|---|---|
| `cli/msgctl/cli.py` | Append one `project` subparser block (`project <dir>`, `set_defaults(handler=cmd_project)`) before `return parser`, and one `cmd_project` handler after `cmd_send`. Nothing else touched. |

**Read-only (do NOT modify):** `cli/msgctl/append.py`, `cli/msgctl/workspace.py`,
`cli/msgctl/errors.py`, all of `server/msgd/core/`. **Untouched:** `cli/pyproject.toml` (sqlite3 is
stdlib — zero new deps), root `pyproject.toml`, `uv.lock`, CI.

---

## 4. Step-by-step (all `python-engineer`)

**Step 1 — `projection.py` constants + schema.**
- `PROJECTION_VERSION: Final = 1` (comment: bump on any schema *or* handler change).
- `PROJECTION_DB_NAME: Final = "projections.sqlite3"`.
- `_SCHEMA` DDL string (the three `CREATE TABLE` + the one index, Ruling 2).
- `_init_schema(conn)`: `executescript(_SCHEMA)` then upsert `meta` version. `_rebuild_schema(conn)`:
  `DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS stream_cursors;` then `_init_schema`-equivalent
  recreate + set version (keep the seam a single function ENG-59 calls).

**Step 2 — `open_db(db_path) -> sqlite3.Connection`.**
- `conn = sqlite3.connect(db_path)`; leave `journal_mode` default (`DELETE`), default `synchronous`.
- Ensure `meta` exists (create-if-absent), read `projection_version`:
  - absent → fresh: `_init_schema` (creates all tables, writes version).
  - present == `PROJECTION_VERSION` → nothing (schema already present).
  - present != `PROJECTION_VERSION` → `_rebuild_schema` (auto-rebuild; cursors now empty).
- Return the connection. (Version decision is made here so `cmd_project` stays a straight-line
  "open → project → dump".)

**Step 3 — read-only log walk `_read_stream_events(stream_dir) -> list[Envelope]`.**
- `for path in sorted(stream_dir.glob("*.ndjson")):` read bytes; `split(b"\n")`; the final element
  (non-empty, no trailing `\n`) is a torn line → **skip without truncating**; each other non-empty
  element → `json.loads` then `Envelope.model_validate`, wrapping either failure as `CorruptLogError`
  (Ruling 7). Accumulate; assert `server.server_sequence == prev+1` (first `==1`) else `CorruptLogError`.
- Returns the ordered `Envelope`s for the stream (full history — the caller applies only the tail
  beyond the cursor).

**Step 4 — apply.**
- `_apply_message_created(conn, env) -> None`: read `message_id, text, format, thread_root_id` from
  `env.body.payload` (validate via `MessageCreatedV1(**payload)` for a clean error on a malformed
  known payload), then `INSERT OR IGNORE INTO messages (...) VALUES (...)` with `stream_id`,
  `server_sequence`, `author_user_id` from `env.body`/`env.server`.
- `_HANDLERS = {("message.created", 1): _apply_message_created}`.
- `project(ws, conn) -> ProjectResult`: `for stream_id in sorted(ws.streams):`
  read `last_applied = SELECT last_applied_seq FROM stream_cursors WHERE stream_id=?` (default 0);
  `events = _read_stream_events(ws.stream_dir(stream_id))`; `new = [e for e in events if
  e.server.server_sequence > last_applied]`; if none, continue; **in one `with conn:` transaction**:
  for each `e` in `new`, `handler = _HANDLERS.get((e.body.type, e.body.type_version))`; if handler →
  apply (counts `applied`), else `skipped += 1`; then upsert the cursor to
  `max(e.server.server_sequence for e in new)` (== last event's seq, ascending). Return counts +
  per-stream head + a `rebuilt` flag threaded from `open_db` if you surface it.

**Step 5 — `dump_messages(conn) -> str`** (Ruling 5): the fixed `SELECT ... ORDER BY stream_id,
server_sequence`, each row → compact JSON object (fixed key order, `ensure_ascii=False`), `\n`-joined.

**Step 6 — `cli.py` append (surgical).**
- After the `send` subparser block, before `return parser`, add the `project` subparser
  (`project <dir>`, `set_defaults(handler=cmd_project)`).
- After `cmd_send`, add `cmd_project(args)`: `ws = Workspace.open(args.dir)`;
  `conn = open_db(ws.root / PROJECTION_DB_NAME)`; `result = project(ws, conn)`; `conn.close()`;
  `print(json.dumps(result-summary))`; `return 0`. Errors already funnel through `main`'s
  `except MsgctlError` (CorruptLogError is a subclass) → `msgctl: …` + exit 1.
- Do not touch `init`/`send`/`main`/`build_parser` ordering beyond the appended block (§6).

**Step 7 — Local gates:** `uv run ruff check`, `ruff format --check`, `uv run mypy`, `uv run pytest`
all green; by hand: `msgctl init`, a few `send`s, `msgctl project`, run `project` again (no-op),
inspect the DB / `dump_messages`.

---

## 5. Test plan (`cli/tests/`, `tmp_path`; subprocess via existing `conftest.run_cli` for real process
boundaries, in-process `project(ws, conn)` calls where a DB handle is needed)

- **`test_project_creates_messages`** — init + N sends → `project` → `messages` has N rows with correct
  `message_id/text/format/stream_id/server_sequence/author_user_id`; DB is at `<dir>/projections.sqlite3`
  (top-level, **not** under `streams/`).
- **`test_incremental_idempotent`** (AC) — `project`; capture `dump_messages`; `project` again with no
  new sends → dump byte-identical, cursor unchanged, row count unchanged (re-run is a true no-op).
  Then one more `send` → `project` → exactly one new row, cursor advanced by one.
- **`test_unknown_type_skips_and_advances`** (AC, D9) — real send (seq 1), hand-write a terminated
  `widget.exploded`/v7 event at seq 2 (test_scan_integrity pattern), real send (seq 3); `project` →
  `messages` has the two real messages, **not** the widget; cursor == 3; the unknown line is still in
  the log **byte-identical** (read-only). Parametrize a second case: `message.created` **v2** (above-max
  version) → same skip+advance (D9 version rule).
- **`test_version_bump_auto_rebuild`** (AC) — `project` at version 1; monkeypatch/patch
  `projection.PROJECTION_VERSION` to 2 (or write `meta.projection_version='0'` to simulate a stale DB);
  re-open + `project` → tables were dropped and fully replayed, final `dump_messages` equals a
  from-scratch projection's dump, and `meta.projection_version` now reads the new value.
- **`test_crash_mid_apply_converges`** — two streams; inject a failure (monkeypatch the second
  stream's handler to raise, or `conn` to fail) after the first stream's transaction commits and
  before the second's; assert the first stream committed, the second did **not** (its cursor still 0,
  no rows); re-run `project` with the fault removed → both streams complete, no duplicate rows, dump ==
  clean-run dump. (Demonstrates per-stream atomicity + `OR IGNORE` convergence.)
- **`test_month_boundary`** — force two month files for one stream (a send, then a send with a mocked
  `server_received_at` in the next month — reuse ENG-57's month-boundary technique); `project` →
  contiguous `server_sequence` across the boundary, all rows present, cursor == last seq.
- **`test_log_read_only`** — snapshot every `streams/**/*.ndjson` byte before `project`; run `project`;
  assert every log file is byte-identical afterward (projection never truncates/repairs/writes the log).
  Include a torn-trailing-line variant: append a partial (no-`\n`) line, `project` → succeeds, ignores
  the torn bytes (that message absent), **and leaves the torn bytes in place** (a later `send` fixes
  them, per ENG-57).
- **`test_corrupt_terminated_line_hard_errors`** — a terminated non-envelope line → `project` exits 1,
  `msgctl:` stderr, no traceback, log untouched (mirrors ENG-57's scan-integrity contract, now via the
  projection reader).
- **`test_projection_determinism.py`**:
  - `test_two_workspaces_identical_dump` — build two workspaces, replay the *same* sequence of sends
    into each (fixed `--event-id`/`--author-*`/text so bodies match), `project` both → `dump_messages`
    byte-identical.
  - `test_rebuild_equals_incremental` — one workspace: incremental `dump` after several `project`s vs.
    a forced full rebuild's `dump` (via the version-bump/`_rebuild_schema` path) → identical. This is
    the local stand-in for ENG-61's gate; it also proves `dump_messages` is order-stable.

Map to ACs: incremental-idempotent → `test_incremental_idempotent`; unknown-type skip + stays in log →
`test_unknown_type_skips_and_advances` + `test_log_read_only`; version-bump auto-rebuild →
`test_version_bump_auto_rebuild`. Determinism/crash/month-boundary/read-only exceed the bare ACs but
are the ticket's stated test plan.

---

## 6. Coordination with ENG-60 (`verify`) — partition & cli.py collision protocol

**ENG-58 owns:** `cli/msgctl/projection.py`, the SQLite schema, the `project` subcommand wiring in
`cli.py` (one subparser block + one handler), `cli/tests/test_projection*.py`.
**ENG-58 does NOT own / must not modify:** any verify logic, `cli/msgctl/verify.py`, and — critically —
**`append.py`/`workspace.py`/`errors.py` are read-only** for this ticket. No shared reader is
extracted; projection duplicates a minimal read-only walk (Ruling 7). If a reviewer asks to "DRY up"
the two scan readers, **push back**: ENG-57's `_scan_stream` mutates (truncates torn lines) and is
shaped for append, not read-only ordered projection — unifying them is a cross-ticket refactor, out of
scope here, and would touch a file this ticket must not own.

**cli.py collision protocol (both tickets append a subcommand):**
- Keep the `cli.py` diff **append-only**: add your subparser block at the **end** of `build_parser`
  (immediately before `return parser`) and your `cmd_*` at the **end** of the handler functions.
  Dispatch is via `set_defaults(handler=…)` + the existing `args.handler(args)` in `main` — there is
  **no separate dispatch line** to edit, so the only collision surface is the two adjacent appended
  blocks.
- **Second-to-merge rebases mechanically:** move your appended block below the other ticket's block;
  because both are self-contained `subparsers.add_parser(...); set_defaults(...)` groups plus an
  independent `cmd_*` function, there is no semantic conflict — a pure reorder. Do **not** refactor
  the parser structure to "make room"; that would turn a trivial rebase into a real merge.

---

## 7. Risks / open questions

- **Full log re-read every run (O(n)).** Applies are incremental (cursor + `OR IGNORE`); the *read*
  is a full scan. Fine at M0; the future fix (persist a byte offset with the cursor) is noted and
  deferred — do not build it now.
- **`_read_stream_events` duplicates ~30 lines of ENG-57 scan logic.** Deliberate (Ruling 7): the
  alternative is either mutating the log (ENG-57's reader truncates) or a cross-file refactor this
  ticket cannot own. If M1 introduces a genuine shared read-only reader in `core/`, both call sites
  migrate then — a separate ticket.
- **Projection durability is intentionally weak** (no fsync, `journal_mode=DELETE`). Correct because
  the projection is rebuildable from the log; a torn projection write is a no-op recovered by the next
  `project`/`rebuild`. Flag so a reviewer doesn't import ENG-57's fsync discipline here.
- **`message.created` v2 handling is untested against a *real* v2** (none exists yet). Covered by the
  synthetic above-max-version case, which is exactly what D9 rule 3 specifies; a real v2 handler +
  `PROJECTION_VERSION` bump is a future ticket.
- **Rebuild seam is exercised only via the auto path** in this ticket. ENG-59 will add the
  user-facing `rebuild` subcommand over `_rebuild_schema`; the seam's signature
  (`_rebuild_schema(conn)` then `project(ws, conn)`) is fixed here so ENG-59 is a thin CLI append with
  no new projection logic. Flag for the ENG-59 planner.
- **WAL deferred.** Single-writer M0 needs no concurrency; WAL's persistent sidecars would complicate
  the "excluded from export/verify by path" guarantee. If a later ticket adds WAL it must also extend
  verify/export to skip `-wal`/`-shm`/`-journal` — noted for ENG-60/M4.

---

## 8. Concise summary (for the dispatcher)

- **DB path/schema:** `<dir>/projections.sqlite3` (workspace root, outside `streams/`, single file via
  default rollback journal, excluded from verify/export **by path**). Tables: `messages`
  (`message_id` PK; `stream_id, server_sequence, author_user_id, text, format, thread_root_id?,
  client_created_at, server_received_at`; index on `(stream_id, server_sequence)`), `stream_cursors`
  (`stream_id` PK, `last_applied_seq`), `meta` (`projection_version`). No FTS in M0.
- **Version/rebuild seam (ENG-59):** `PROJECTION_VERSION=1` in code, stored in `meta`, checked in
  `open_db`; mismatch → `_rebuild_schema(conn)` (drop+recreate+reset cursors) then the normal
  `project()` replays from empty cursors. ENG-59's `msgctl rebuild` == `_rebuild_schema` + `project`,
  the same two calls; ship only the auto path.
- **Transaction/idempotency ruling:** one atomic transaction **per stream per run** wrapping that
  stream's row upserts **and** its cursor bump, **plus** `INSERT OR IGNORE` by `message_id`. The
  transaction gives cursor/row atomicity (crash between insert and cursor bump rolls both back);
  `OR IGNORE` guarantees re-apply/replay never duplicates. Both, not either.
- **Determinism contract (ENG-61):** final state is a pure function of the log; apply order fixed
  (`sorted(stream_id)`, then ascending `server_sequence`) but state-irrelevant. ENG-61 diffs a
  **normalized SELECT dump** (`dump_messages(conn)`: explicit columns, `ORDER BY stream_id,
  server_sequence`, compact JSON per row) — **never raw SQLite bytes**. Identical logs → identical
  dump; rebuild-dump == incremental-dump.
- **Unknown types (D9):** only `message.created` v1 projects; unknown types and `message.created` v≥2
  skip the row but **still advance the cursor** (they occupy a sequence). Never crash.
- **Read-only over the log:** projection never truncates/repairs; torn trailing line is invisible (not
  fixed), terminated corruption is a hard `CorruptLogError`. Duplicates a minimal read-only walk;
  does **not** reuse ENG-57's mutating `_scan_stream`.
- **cli.py collision protocol:** both ENG-58 and ENG-60 **append** their subparser block (end of
  `build_parser`) + `cmd_*` (dispatch via `set_defaults(handler=…)`, no separate dispatch line);
  second-to-merge mechanically reorders its block below the other's — a pure rebase, no parser
  refactor. `append.py`/`workspace.py`/`errors.py` stay read-only.

---

## Review Round 1 — Triage & Fix Plan

Reviewer verdict: REQUEST_CHANGES (comment form, own-PR) on PR #8 — 1 blocking, 2 non-blocking.
Reviewer confirms the load-bearing invariants are correct (per-stream transaction wraps rows+cursor,
cursor advances over skipped events, read-only reader, version stamp written *last* in
`_rebuild_schema` so a crashed rebuild re-converges — the classic gate bug is absent). I verified all
three findings against the branch (`projection.py`: `_apply_message_created`'s unwrapped
`MessageCreatedV1(**env.body.payload)`; `dump_messages`'s default `json.dumps` separators; no test
path that re-inserts an existing `message_id`). **All three ADDRESSED** — each fix is small and lands
in one fixup commit. Implementer: `python-engineer`.

| # | Finding | Severity | Decision |
|---|---|---|---|
| 1 | Malformed known payload escapes as raw `ValidationError` traceback | blocking | **ADDRESS — wrap → `CorruptLogError` + test** |
| 2 | `dump_messages` not compact despite the pinned ENG-61 contract | non-blocking | **ADDRESS — add `separators=(",", ":")` now + reimplementation test** |
| 3 | `OR IGNORE` immutability pin unguarded (`OR REPLACE` would pass) | non-blocking | **ADDRESS — first-write-wins unit test** |

### Finding 1 — Malformed `message.created` v1 payload → wrap as `CorruptLogError` — ADDRESS (blocking)

**Ruling: hard error, not skip — and the reviewer is right that it must be a *clean* one.** A
terminated line that is a structurally-valid `Envelope` of a *known* `(type, version)` but whose
payload fails `MessageCreatedV1` is not a D9 case (D9 covers *unknown* types/versions, which
skip-with-cursor-advance). The only M0 writer is `msgctl send`, which validates the payload through
`MessageCreatedV1` *before* writing — so an invalid known payload in the log is corruption a
well-behaved writer never emits, exactly the class Ruling 7 sends to `CorruptLogError`. Plan Step 4
already said "a clean error on a malformed known payload"; the implementation raised the right alarm
through the wrong channel (`ValidationError` is not a `MsgctlError`, so `main` lets it traceback).

**Fix (`projection.py`, `_apply_message_created`) — wrap at the construction site, narrowest scope:**

```python
try:
    payload = MessageCreatedV1(**env.body.payload)
except ValidationError as exc:
    raise CorruptLogError(
        f"invalid message.created v1 payload in stream {env.body.stream_id} "
        f"seq {_server_sequence(env)} (event {env.body.event_id}): {exc}"
    ) from exc
```

`ValidationError` and `CorruptLogError` are already imported (used by `_read_stream_events`) — no
import change. Do **not** widen the except beyond `ValidationError`, and do not wrap the
`conn.execute` (a SQL failure there would be a projection bug, not log corruption — let it surface).
The raise unwinds out of the per-stream `with conn:` block, which **rolls back** that stream's
partial batch including the cursor bump — correct, and already the crash-mid-apply contract: nothing
half-applied, earlier streams' commits stand, re-run after the log is fixed converges.

**Test (`cli/tests/test_projection.py`) — `test_malformed_known_payload_is_hard_error`:** init + one
good send (seq 1); hand-craft a *terminated* envelope at seq 2 with `type="message.created"`,
`type_version=1`, valid ids and `event_hash = hash_event(raw_body)` (the scan-integrity crafting
pattern) but `payload` missing `message_id` (e.g. `{"text": "orphan"}`); append it;
`run_cli("project", …)` → exit **1**, stderr starts `msgctl:`, `"Traceback" not in stderr`; log file
byte-identical (read-only holds on the error path too); and the DB shows **no** rows and no cursor
for that stream (the whole per-stream transaction rolled back — seq 1's row must not have committed
with the cursor un-bumped or vice versa). This is the exact sibling of
`test_corrupt_terminated_line_hard_errors` one layer up the stack.

### Finding 2 — `dump_messages` compactness — ADDRESS (fix now, code → contract)

**Ruling: add `separators=(",", ":")` now — do not defer to ENG-61, and do not retreat to
"import-only".** The dump is THE pinned ENG-61 comparison surface, and plan Ruling 5 deliberately
permits ENG-61 to "reimplement the identical query"; leaving the code non-compact while the written
contract (Ruling 5 *and* the function's own docstring) says "compact" guarantees a byte-level
mismatch the moment anyone reimplements faithfully from the contract text. Reconciling toward the
contract (compact) rather than the accident (default separators) is strictly cheaper now than an
equivalence-gate churn after ENG-61 freezes. The reviewer's alternative — tighten the note to "must
import, never reimplement" — is rejected: it would make the documented contract unimplementable from
its own text, which is a worse contract than a two-token code fix.

**Fix (`projection.py`, `dump_messages`):** add `separators=(",", ":")` to the `json.dumps` call.
The docstring already says compact — now true; append one line: "ENG-61 may import this function or
reimplement the identical query + this exact serialization (compact separators, fixed
`_DUMP_COLUMNS` key order, `ensure_ascii=False`)."

**Test (`cli/tests/test_projection_determinism.py`) — `test_dump_matches_reimplementation`:** after a
`project`, rebuild the expected dump *independently* from the same DB — run the contract's SELECT,
serialize each row dict with `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`,
`\n`-join — and assert byte-equality with `dump_messages(conn)`. This is precisely the faithful
ENG-61 reimplementation the contract licenses, so it pins the separators (and any future
serialization drift) — rather than a fragile "no `', '` substring" assertion, which message text
could legitimately contain.

### Finding 3 — `OR IGNORE` first-write-wins guard — ADDRESS (add the cheap pin)

**Ruling: add it.** The reviewer is correct that it is not a live M0 bug (rows are immutable and the
dump never touches rowid, so `OR REPLACE` is observationally identical today) — but Ruling 4 chose
`IGNORE` over `REPLACE` *deliberately*, and an unguarded deliberate ruling is exactly what a future
"harmless" edit flips. The pin is one small unit test with no new machinery, and it becomes
load-bearing the moment any real re-apply path exists (ENG-59 rebuild over a non-empty table, a
future concurrent projector).

**Test (`cli/tests/test_projection.py`) — `test_reinsert_existing_message_id_keeps_first_row`:**
in-process: `open_db` on a tmp path; apply a crafted envelope via `_apply_message_created` inside a
`with conn:`; then apply a *second* crafted envelope with the **same `message_id`** but different
`text` (and a different `server_sequence`); assert the stored row still carries the **first** text
and sequence (first-write-wins), row count is 1, and `dump_messages` is byte-identical between the
two applies. `OR REPLACE` fails the first-text assertion; a dropped `OR IGNORE` fails on
`IntegrityError`. (The same-id/different-body input is synthetic — no M0 writer produces it — which
is fine: the test pins the SQL conflict clause, not a log scenario; say so in its docstring so
nobody "fixes" the test by validating the input away.)

### Net change scope

- `cli/msgctl/projection.py` — try/except around `MessageCreatedV1(**…)` → `CorruptLogError` (F1);
  `separators=(",", ":")` in `dump_messages` + one docstring line (F2).
- `cli/tests/test_projection.py` — `test_malformed_known_payload_is_hard_error` (F1),
  `test_reinsert_existing_message_id_keeps_first_row` (F3).
- `cli/tests/test_projection_determinism.py` — `test_dump_matches_reimplementation` (F2).

No plan rulings overturned — F1 enforces Ruling 7's clean-error contract at the payload layer, F2
makes the code match Ruling 5's already-written contract, F3 pins Ruling 4's `IGNORE`-over-`REPLACE`
choice. **Note for ENG-61 (record now):** the dump contract is finalized as **compact** separators —
the equivalence gate compares against `dump_messages` output or a byte-faithful reimplementation per
the F2 docstring line. No `core/`/`append.py`/`workspace.py`/`errors.py` edits, no new deps. One
fixup commit; re-run local gates (`ruff check`, `ruff format --check`, `mypy`, `pytest`) before
pushing, then reply on the three review threads with these dispositions.
