# ENG-57 — M0: `msgctl` CLI skeleton + NDJSON append (`message.created`)

**Milestone:** M0 — Protocol spike
**Tech-lead:** planning complete; all implementation is **`python-engineer`**.
**TDD refs:** §1.1 (repo layout — `cli/` is the M0 spike, `core/` shared), §2.1 (envelope: body vs. server split, stored form), §2.2 (`message.created` v1 payload; first event = sequence 1), §3.1 (per-stream gapless+monotonic `server_sequence`, D2), §3.2 (upload semantics + idempotency by `event_id`), §9 (export/import NDJSON layout — the tree shape we mirror), §11/§13 (M0 exit). Locked decisions: **D1** (hash over JCS(body) only), **D2** (gapless per-stream sequence), **D9** (unknown types preserved-not-crashed), **D14** (`client_created_at` untrusted).
**Depends on (all merged to main):** ENG-53 (scaffold, `msgctl` console script), ENG-54 (`envelope.py`, `payloads/`, `ids.py`), ENG-55 (`jcs.py`, `MAX_DEPTH=128`), ENG-56 (`hashing.py`: `hash_event`/`verify_hash`, frozen vectors).

> **Note on the "§11" pointer in the task:** the export *layout* the ticket says to mirror is defined in **TDD §9** (Export / import). §11 is Deployment. This plan mirrors the §9 tree (`streams/<stream_id>/<YYYY-MM>.ndjson`, one full envelope per line, keyed by `stream_id` never name). All references below to "the export layout" mean §9.

---

## 1. Goal (restated)

`msgctl` becomes the **M0 stand-in for the M1 sync server's sequencer**: a local, file-backed
implementation of the exact same envelope + per-stream sequencing contract, so the protocol
(`core/`) is exercised end-to-end before any server exists. Two commands:

- **`msgctl init <dir>`** — materialize a workspace folder: mint a workspace ULID + a single local
  author identity, write the manifest/stream-registry, and lay down the `streams/` tree in the §9
  export shape (empty until first send).
- **`msgctl send <dir> --stream <name> --text "…"`** — build a `message.created` envelope via
  `core/` (client-minted ULIDs, client `client_created_at`), compute `event_hash`, assign the next
  gapless per-stream `server_sequence` locally (the sequencer stand-in mints `server` metadata),
  and append exactly one JSON line to the stream log.

The append path is **crash-safe** (a torn write can never be accepted) and **idempotent by
`event_id`** (re-append is a no-op returning the original record), mirroring §3.2. The log **is**
the source of truth — sequencing and idempotency state are derived by scanning it on open; there is
no sidecar index in M0.

Areas touched: `cli/msgctl/` (new modules + `cli.py` rewrite), `cli/tests/` (new tests). **No
`core/` edits, no new runtime dependencies, no server/web/CI changes.**

---

## 2. Design rulings (each ticket question, ruled)

### Ruling 1 — Workspace folder layout & registry

**Layout (created by `init`, extended by `send`):**

```text
<dir>/
  workspace.json                       # manifest + stream registry (name -> id); atomically written
  .lock                                # workspace-level advisory lock (registry mutations)
  streams/
    <stream_id>/                       # keyed by stream_id (rename-safe, §9), never by name
      .lock                            # per-stream advisory lock (append critical section)
      2026-07.ndjson                   # month-partitioned log; month = server_received_at (§9)
```

- **Stream log tree is byte-for-byte the §9 export shape** — `streams/<stream_id>/<YYYY-MM>.ndjson`,
  one full envelope per line, ascending `server_sequence`, month split by `server_received_at`.
  Chosen over an M0 single-`events.ndjson` simplification **specifically so ENG-58/59/60 (project /
  rebuild / verify) and M4 export read the same tree with no special-casing**: M4 export becomes
  "copy `streams/`, synthesize `manifest.json`, add `blobs/`+`users.json`". Lexical sort of
  `YYYY-MM.ndjson` is chronological, so the scan (Ruling 3) walks month files in order trivially.
- **Manifest file is `workspace.json`, deliberately NOT `manifest.json`.** A live workspace is not
  an export: §9's `manifest.json` carries head_seq, event counts, blob index, and export time — an
  export is a produced artifact. Using a distinct name prevents a future `verify`/`import` from
  mistaking a live workspace for an export. The `streams/` subtree is identical; only the top-level
  manifest differs. Documented as the forward-compatible M0 subset below.
- **Registry lives in `workspace.json`** as a `streams` object keyed by `stream_id`:

  ```json
  {
    "format_version": 1,
    "workspace_id": "w_01JZ…",
    "name": "<dir basename, or --name>",
    "created_at": "2026-07-04T18:22:10.123Z",
    "local_author": { "user_id": "u_01JZ…", "device_id": "d_01JZ…" },
    "streams": {
      "s_01JZ…": { "name": "general", "kind": "channel", "created_at": "…" }
    }
  }
  ```

  The **name→id index is derived on load** by inverting `streams` (stream names are unique within a
  workspace; a duplicate name in the file is a corruption error). **`head_seq` is intentionally NOT
  stored** in the manifest — the log is the single source of truth for sequence (Ruling 3), so a
  denormalized head would be a second source that can desync. M4 export computes head_seq by
  scanning. This is the one documented divergence from §9's manifest and it is strictly safer.
- **Manifest atomicity (its own concern):** every manifest write is `write temp file in <dir> →
  flush + `os.fsync` → `os.replace(tmp, workspace.json)`` (atomic rename on POSIX), performed while
  holding the workspace `.lock`. A crashed manifest write therefore leaves the prior manifest intact.
- **M0 subset of §9 manifest (forward-compatible):** omits `blobs/` index (no files in M0),
  `users.json` (single local author lives inline in `local_author`), per-stream `head_seq` and event
  counts (derived), and export time (not an export). Same `format_version` key and same
  `streams`-keyed-by-id/name/kind shape, so growth is additive.

### Ruling 2 — What exactly is one log line

One line = one **full stored `Envelope`** exactly as §2.1 draws the stored form and §9 requires
("one NDJSON line = one full envelope"):

```json
{"body":{…},"event_hash":"sha256:…","signature":null,"server":{"server_sequence":9284,"server_received_at":"…","payload_redacted":false}}
```

- The CLI, as the sequencer stand-in, **mints the `server` block locally**: `server_sequence` = next
  gapless per-stream value (Ruling 3), `server_received_at` = now (RFC 3339 `…Z`), `payload_redacted`
  = `false`. `signature` = `null` (D1, reserved).
- **Serialization:** `line = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False,
  separators=(",", ":")) + "\n"`. Compact, one physical line (json escapes any embedded newline in
  string values, so NDJSON framing is never broken). No canonicalization for *storage* — JCS is only
  for the hash.

**Hash-construction path — RULED: model-is-source (build via `core/`, hash the model dump).**
ENG-56 licenses this explicitly: `hash_event(body)` must take the *raw* body for the **upload**
path (untrusted client bytes may diverge under Pydantic lax coercion), but `verify_hash(envelope)`
and hashing `body.model_dump()` are **exact when the `Body` IS the source of truth** — "client-side
construction (`build_message_created_body`), tests, and re-hashing an event you built yourself." The
CLI is precisely that client-side construction case: it mints the event itself; there are no
pre-existing untrusted bytes to be faithful to. So:

1. `body = build_message_created_body(workspace_id=…, stream_id=…, author_user_id=…,
   author_device_id=…, client_created_at=…, text=…, format=…, event_id=…?)` — the `core/` convenience
   that mints `event_id`/`message_id`, validates through `MessageCreatedV1`, and returns a `Body`.
2. `event_hash = hash_event(body.model_dump(mode="json"))` — `body` is source-of-truth, so
   `model_dump` is byte-faithful and this digest is exactly what a re-hash of the stored line
   reproduces.
3. `envelope = Envelope(body=body, event_hash=event_hash, signature=None,
   server=ServerMetadata(server_sequence=…, server_received_at=…, payload_redacted=False))`.

Because the stored `body` bytes come from `model_dump` of a source-of-truth `Body`, reloading the
line (`Envelope.model_validate(json.loads(line))`) and calling `verify_hash` re-dumps to the same
JCS and returns `True` — satisfying both the "verify_hash green" and "round-trips through the model"
acceptance criteria. We do **not** hand-hash a plain dict here; the `core/` builder is the licensed,
tested path and keeps the CLI from re-implementing payload assembly.

### Ruling 3 — Sequencing + idempotency state → **scan the log on open, no sidecar index**

Source of truth = the log itself (§9). On entering the append critical section for a stream, scan
all its month files (`streams/<sid>/*.ndjson`, lexically sorted = chronological) once:

- Read the file; a **line is "accepted"** only if it is terminated by `\n`. Split on `\n`; the final
  element (bytes after the last `\n`, if non-empty) is a **torn trailing line** — see torn policy.
- For each accepted (terminated) line: `Envelope.model_validate(json.loads(line))`; accumulate
  `event_id` into a set and track the running `server_sequence`. Assert contiguity (`seq == prev+1`,
  first == 1) as an integrity check.
- **Next sequence** = `last_accepted_sequence + 1`, or **1** for an empty stream (matches §2.2 "first
  event, sequence 1"). Gapless + monotonic **across process restarts** because it is always re-derived
  from the last accepted line — no persisted counter to drift.
- **Idempotency:** if the target `event_id` is already in the scanned set → **no-op**: do not write,
  do not consume a sequence; return the *original* stored line (mirrors §3.2 "returns the original
  accepted record, not an error and not a duplicate"). `send` mints a fresh `event_id` by default so
  this is normally unreachable from `send`; the `--event-id` flag (Ruling 5) makes an outbox-style
  retry explicit and testable.

O(n) open is **ruled acceptable at M0 scale** (a local single-actor spike). No sidecar index.

**Torn-line policy (precise):**
- Definition of accepted: written **and** newline-terminated. The append writes the full line
  *including* its trailing `\n` in one `write`, then `flush` + `os.fsync`, and only *then* is the
  event acknowledged (printed to stdout, its sequence consumed). A torn write (crash mid-`write`, or
  after bytes but before the process could fsync/return) is therefore **never acknowledged**.
- On open, if a month file's last byte is not `\n` (non-empty file), the trailing partial bytes are a
  crashed write. **Repair by truncation:** `f.truncate(<offset just past the last '\n'>)`, and emit a
  one-line warning to **stderr** (`warning: dropped torn trailing line in <path>`). Truncation (not
  mere in-memory skipping) is required: leaving the partial bytes would concatenate them with the next
  append into one corrupt physical line. After truncation the file is a clean run of whole lines.
- The dropped torn line was never acknowledged, so **its would-be sequence is simply reused by the
  next real append → no gap, no monotonicity break, no torn acceptance.** Only the newest month file
  can carry a torn trailing line; earlier months are complete (checked anyway, cheap).
- A **terminated** line that fails `json.loads` is corruption (our writer never emits it) → **hard
  error** (exit 1), do not silently skip — skipping a whole terminated line could mask real data loss
  and break gaplessness. A terminated line that is valid JSON of an **unknown event type** is a real
  accepted event → counted toward the sequence and preserved (D9); M0 only *sends* `message.created`
  but the scan must not choke on other types.

### Ruling 4 — Locking for concurrent `msgctl` processes → **flock (advisory), per-stream + workspace**

**Ruled: `fcntl.flock` advisory locks. Sequences must never fork; flock is cheap and honest** — the
single-process assumption is not written down anywhere and a second `msgctl send` racing the first
would otherwise compute the same "next" sequence off a stale scan.

- **Per-stream exclusive lock** `streams/<sid>/.lock` (dedicated file, `os.open(O_CREAT|O_RDWR)` +
  `fcntl.flock(fd, LOCK_EX)`) wraps the whole **scan → idempotency check → append (write+flush+fsync)**
  critical section. Two concurrent sends to the same stream serialize; the second scans *after* the
  first's line is durable, so it computes `n+1`. No fork.
- **Workspace-level exclusive lock** `<dir>/.lock` wraps **registry mutations** only — creating a new
  stream (mint `s_` id, `streams` insert, atomic manifest rewrite). Plain appends to an existing
  stream do **not** touch the manifest (head_seq isn't stored, Ruling 1), so they need only the stream
  lock — keeping the common path cheap.
- **Lock ordering** to prevent deadlock: always workspace lock **before** stream lock when both are
  needed (auto-create + first append); release in reverse.
- **POSIX-only:** `fcntl.flock` is POSIX. msg runs on Linux (server image) and macOS (dev); the
  `.lock` files are advisory and self-cleaning (left as empty files, harmless). Windows is out of
  scope for M0 (documented risk). Locks are released by closing the fd / process exit.

### Ruling 5 — CLI UX

- **argparse subcommands** on the existing `msgctl` entry point (`cli.py:main`). `--version` is
  preserved on the top-level parser. Two subparsers:
  - `init <dir> [--name NAME]` — positional workspace dir (matches ticket signature).
  - `send <dir> --stream NAME --text TEXT [--format {markdown,plain}] [--event-id ID]
    [--author-user-id U] [--author-device-id D]` — positional dir + required `--stream`/`--text`.
    `--event-id` enables idempotent retry (Ruling 3). Author flags default to the workspace's
    `local_author`; overridable for tests.
- **`<dir>` is positional**, matching the ticket exactly (`msgctl init <dir>`, `msgctl send <dir> …`).
  A global `--workspace-dir` is deliberately *not* added now (would create two ways to name the dir);
  noted as a trivial future addition if ops ergonomics want it.
- **Output:** on success, the **full accepted stored envelope** (the exact appended line) is printed
  as one JSON object to **stdout** (pipeable / inspectable). On an idempotent no-op, the *original*
  record is printed identically (callers cannot distinguish, per §3.2), with an informational
  `idempotent: event_id already present` note to **stderr**. `init` prints the created workspace
  manifest (or a `{workspace_id, dir}` summary) to stdout.
- **Exit codes:** `0` success; `1` operational error (workspace missing/not-initialized, corrupt
  terminated line, duplicate stream name in manifest, unresolvable stream on a read-only op); `2`
  argparse usage error (argparse default). Errors are printed to stderr as `msgctl: <message>`.
- **`init` idempotence/safety:** `init` on a non-empty/existing workspace dir is an **error** (exit 1,
  "workspace already initialized") rather than clobbering — no accidental re-mint of `workspace_id`.

### Ruling 6 — Dependencies → **confirmed: none added**

`cli/pyproject.toml` gains **no new runtime deps.** Everything used is stdlib (`argparse`, `json`,
`fcntl`, `os`, `datetime`, `pathlib`) plus `msgd` (already the sole declared dependency). ULID
minting, envelope models, payload builder, and hashing all come from `msgd.core`. Confirmed against
the current `cli/pyproject.toml` (`dependencies = ["msgd"]`). No `uv.lock` change.

---

## 3. File list

**Create (all `cli/msgctl/`):**

| File | Purpose |
|---|---|
| `cli/msgctl/workspace.py` | Layout constants + paths; `Workspace` dataclass; manifest read + atomic write; stream-registry load/resolve/auto-create; RFC 3339 `now()`; `init_workspace()`. Owns `workspace.json` and the name→id index. |
| `cli/msgctl/append.py` | The append engine: `fcntl` lock helpers (per-stream + workspace), month-file path resolution, **scan-on-open** (sequence + `event_id` set + torn-line truncation + contiguity check), **idempotent `append_event(ws, stream_id, envelope) -> (record, appended: bool)`**, write+flush+fsync. |
| `cli/msgctl/errors.py` | `MsgctlError(Exception)` carrying an exit code; subtypes `WorkspaceError`, `CorruptLogError`, `StreamError`. `cli.py` maps these to stderr + exit code. |

**Modify:**

| File | Change |
|---|---|
| `cli/msgctl/cli.py` | Replace the single-parser stub with argparse subcommands (`init`, `send`) + handlers; keep `--version`; wire `errors.py` → exit codes; JSON stdout output. |

**Create (tests, `cli/tests/`):** `test_init.py`, `test_send.py`, `test_sequencing.py`,
`test_torn_write.py`, `test_idempotency.py`, `test_concurrency.py` (see §5). Existing
`cli/tests/test_cli.py` (version + import edge) stays as-is.

**Untouched:** all of `server/` (`core/` is consumed, never edited), `cli/pyproject.toml`, root
`pyproject.toml`, `uv.lock`, CI.

---

## 4. Step-by-step (all `python-engineer`)

**Step 1 — `workspace.py`.**
- Constants: `MANIFEST_NAME = "workspace.json"`, `STREAMS_DIR = "streams"`, `FORMAT_VERSION = 1`,
  `WORKSPACE_LOCK = ".lock"`, `STREAM_LOCK = ".lock"`.
- `now_rfc3339() -> str`: `datetime.now(timezone.utc)` → `…Z`, millisecond precision (matches the
  §2.1 example shape; `_validate_rfc3339` is shape-only so exact precision is free choice — pick ms).
- `Workspace` dataclass: `root: Path`, `workspace_id`, `name`, `local_author (user_id, device_id)`,
  `streams: dict[str, StreamInfo]`. `@classmethod open(root)` → read+parse `workspace.json`, build
  the `name→id` index (raise `WorkspaceError` if missing/not-initialized; `CorruptLogError` on
  duplicate stream name). `write_manifest()` → temp-file + fsync + `os.replace` (caller holds
  workspace lock).
- `init_workspace(root, name=None)`: error if `workspace.json` already exists; else `mkdir -p
  root/streams`, mint `workspace_id = ids.new_workspace_id()`, `local_author = (ids.new_user_id(),
  ids.new_device_id())`, write manifest with empty `streams`. Return the `Workspace`.
- `resolve_or_create_stream(ws, name, *, kind="channel") -> stream_id`: under **workspace lock**,
  reload manifest (fresh), return existing id for `name`, else mint `s_` id, `mkdir
  streams/<sid>`, insert into `streams`, atomic manifest rewrite, return id. (Auto-create is an M0
  convenience; in M1 a stream is born from a `channel.created`/`dm.created` `workspace-meta` event —
  note this in the docstring.)

**Step 2 — `append.py`.**
- `@contextmanager flock_exclusive(path)`: `os.open(path, O_CREAT|O_RDWR, 0o644)` →
  `fcntl.flock(fd, LOCK_EX)` → yield → `flock(LOCK_UN)` + `os.close` in `finally`.
- `_month_file(stream_dir, server_received_at) -> Path`: `f"{yyyy}-{mm}.ndjson"` from the timestamp.
- `_scan_stream(stream_dir) -> ScanResult(last_seq: int, event_ids: dict[str, str])`: for each
  `sorted(glob("*.ndjson"))`, open `r+b`; if non-empty and last byte != `\n`, find last-`\n` offset
  and `truncate` (torn repair) + stderr warning; iterate whole lines, `Envelope.model_validate`,
  record `event_id → raw_line`, verify `server.server_sequence == running+1` (first==1) else
  `CorruptLogError`; a terminated line failing `json.loads` → `CorruptLogError`. Return last seq +
  the id→line map (map value lets the idempotent path return the original record).
- `append_event(ws, stream_id, *, build_envelope) -> AppendResult`: acquire **stream lock**; scan;
  if `build_envelope` needs the next seq, compute `next_seq = last+1` and `server_received_at = now`,
  then `build_envelope(next_seq, server_received_at)` returns the `Envelope`; if
  `envelope.body.event_id` already in `event_ids` → return `(original_line, appended=False)` (no
  write); else serialize (Ruling 2), open the month file `ab`, `write(line)`, `flush`,
  `os.fsync(fileno)`, return `(line, appended=True)`. (Keeping `build_envelope` as a callback lets the
  sequence/timestamp be minted *inside* the lock, so two racers never mint the same seq.)

**Step 3 — `cli.py`.**
- `build_parser()` with subparsers; `cmd_init(args)` and `cmd_send(args)` handlers.
- `cmd_send`: `ws = Workspace.open(dir)`; `stream_id = resolve_or_create_stream(ws, args.stream)`;
  define `build_envelope(seq, recv_at)`: `body = build_message_created_body(workspace_id=ws.workspace_id,
  stream_id=stream_id, author_user_id=args.author_user_id or ws.local_author.user_id,
  author_device_id=…, client_created_at=now_rfc3339(), text=args.text, format=args.format,
  event_id=args.event_id)`; `event_hash = hash_event(body.model_dump(mode="json"))`; return
  `Envelope(body=body, event_hash=event_hash, signature=None,
  server=ServerMetadata(server_sequence=seq, server_received_at=recv_at, payload_redacted=False))`.
  Call `append_event`, print the record JSON to stdout; if not appended, stderr idempotency note.
- `main(argv)`: parse; dispatch; wrap handler in `try/except MsgctlError` → `print("msgctl: …",
  file=sys.stderr)` + `return err.exit_code`. Preserve `--version`.

**Step 4 — Local gates.** `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
`uv run pytest` all green; run `msgctl init` + a couple of `send`s by hand and eyeball a line +
`verify_hash`.

---

## 5. Test plan (`cli/tests/`, tmp dirs via `tmp_path`) — every AC as an explicit test

Restart / concurrency tests invoke the CLI **as a subprocess** (`subprocess.run([sys.executable,
"-m", "msgctl.cli", …])` or the installed `msgctl`) so process boundaries and flock are real, not
faked. Non-restart tests may call `main([...])` in-process for speed.

- **`test_init.py`** — `init` creates `workspace.json` (valid `format_version`, `w_` id, empty
  `streams`, `local_author` with `u_`/`d_` ids) and `streams/`; re-`init` on an initialized dir exits
  1.
- **`test_send.py` (AC: verify_hash green + model round-trip):** send → one line appended to
  `streams/<sid>/<YYYY-MM>.ndjson`; **`Envelope.model_validate(json.loads(line))` succeeds** and
  **`verify_hash(envelope) is True`** (headline hash AC); assert `server.server_sequence == 1`,
  `signature is None`, `payload_redacted is False`, `type == "message.created"`, payload `text`
  matches; stdout is that same JSON. A helper `assert_every_line_verifies(stream_dir)` re-runs
  `verify_hash` over **every** appended line and is reused by all send-producing tests.
- **`test_sequencing.py` (AC: gapless + monotonic across restarts):** N sends **each in a fresh
  subprocess** to one stream → sequences are exactly `1..N`, strictly increasing, no gaps, N distinct
  `event_id`s, N lines. A second stream sequences independently from 1 (per-stream, D2). Re-scan after
  the last restart confirms `next == N+1`.
- **`test_torn_write.py` (torn-write safety):** send a few events; **manually truncate the month file
  mid-line** (drop the trailing `\n` and some bytes of the last line); then `send` again → the torn
  line is dropped (file no longer contains it), the new event reuses that sequence (**no gap**), total
  lines consistent, and **the torn partial was never counted as accepted** (its `event_id` absent).
  Also assert `verify_hash` green over all surviving lines and no `CorruptLogError`.
- **`test_idempotency.py` (AC: duplicate `event_id` doesn't duplicate a line):** `send … --event-id
  E` twice → exactly **one** line for `E`; second call's stdout equals the first's record byte-for-
  byte; `server_sequence` unchanged; the sequence counter did **not** advance (a following default
  send gets the next contiguous number, not a gap).
- **`test_concurrency.py` (flock, no sequence fork):** launch **two subprocesses** each sending K
  events to the **same** stream concurrently (`subprocess.Popen` ×2, wait both); afterwards sequences
  are exactly `1..2K` with **no duplicates and no gaps**, `2K` distinct `event_id`s, `2K` lines — i.e.
  the two processes never minted the same sequence. (Skip/guard on non-POSIX where `fcntl` is absent.)
- Cross-cutting: a **round-trip** assertion — for a produced line, `Envelope.model_validate(...)` then
  `model_dump(mode="json")` deep-equals the parsed stored JSON (structural, per ENG-54's ruling), and
  `verify_hash` is True. Wired into `test_send.py` via the shared helper.

Map to ACs: verify_hash green → `test_send`; gapless+monotonic across restarts → `test_sequencing`;
duplicate `event_id` no dup → `test_idempotency`; round-trip through models → `test_send`/round-trip
helper. Torn-write & concurrency exceed the ACs but are required by the ticket's test plan.

---

## 6. Risks / open questions

- **flock is POSIX-only.** `fcntl` is unavailable on Windows; M0 targets Linux (image) + macOS (dev),
  so acceptable. `test_concurrency` guards on `fcntl` availability. If Windows dev support is ever
  needed, swap the two lock helpers for an `msvcrt`/lockfile shim behind the same interface — no
  caller change. Low residual risk.
- **Month-boundary sequencing.** Sequences must stay gapless *across* month files. The scan
  concatenates all `*.ndjson` in lexical (= chronological) order, so the last accepted line of the
  newest month is the true head. Pinned by a test that forces two month files (write a line, then send
  with a mocked/late `server_received_at` in the next month) — optional but recommended; at minimum
  the scan logic is written to iterate all months, not just the current one.
- **`fsync` performance.** One `fsync` per append is the honest crash-safety cost; at M0 (local, human
  send rate) it is irrelevant. Kept — the whole point of the ticket is that a torn write is never
  accepted, which requires durability before acknowledgement.
- **Auto-create of streams is an M0 convenience.** M1 replaces it with real `channel.created` /
  `dm.created` `workspace-meta` events (§2.2). Documented in `resolve_or_create_stream`; nothing
  downstream depends on the auto-create staying.
- **`workspace.json` vs export `manifest.json` divergence.** Deliberate (Ruling 1). The load-bearing
  alignment is the `streams/` tree, which is identical to §9; M4 export synthesizes its own
  `manifest.json` (with head_seq computed by scan) and adds `blobs/`+`users.json`. Flag for the M4
  export ticket so it does not assume a live workspace already carries an export manifest.
- **Single local author (no auth in M0).** `init` mints one `u_`/`d_` pair; every `send` authors as
  it. This is the sequencer-stand-in scope; M1 introduces real sessions and the `author == session`
  check (§3.2). No envelope-shape impact.
- **Idempotency scope.** M0 idempotency is per-workspace-log by `event_id` via the scanned set, which
  matches §3.2's "unique per workspace." Because M0 has one local author and no cross-stream event_id
  reuse, checking within the target stream's scan is sufficient; the id set is per-stream. (If a future
  M0 command could resend an event to a *different* stream, idempotency would need a workspace-wide id
  index — out of scope now; noted.)
