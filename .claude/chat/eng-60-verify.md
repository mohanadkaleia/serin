# ENG-60 — M0: `msgctl verify` — hash recomputation + sequence contiguity

- **Linear:** ENG-60 · Milestone M0 — Protocol spike
- **Tech-lead:** planning complete; all implementation is **`python-engineer`**.
- **TDD refs:** §2.1 (envelope; `event_hash` = SHA-256 over JCS(body) only, D1), §2.2/§2.3 (payload schemas, additive), §3.1 (per-stream gapless+monotonic `server_sequence`, D2), §9 (export tree = the layout verify walks), §11.4/§12 (`msgctl verify` re-hashes the verbatim stored dict), §13 (M0 exit). Locked decisions: **D1**, **D2**, **D9** (unknown types preserved-not-crashed), **D14**.
- **Depends on (all merged to main):** ENG-53/54/55/56 (`core/`), **ENG-57** (`msgctl init/send`, the workspace layout + NDJSON tree verify reads).
- **Runs in parallel with ENG-58** (SQLite projection). See §8 (Collision protocol) — the partition is pinned so the two tickets cannot touch the same code.

> This ticket makes "the ownership pitch testable": the M0 log is only trustworthy if a **byte-faithful** re-hash reproduces every stored `event_hash` and every per-stream sequence is gapless. `verify` is the tool that proves it, CI-suitably.

---

## 1. Goal (restated)

`msgctl verify <dir>` walks a workspace's log tree and independently re-derives two invariants that `send` promised, reading nothing but the bytes on disk:

1. **Hash faithfulness (D1):** for **every** event in **every** stream, recompute `event_hash` = SHA-256 over JCS of the **raw** stored `body` and compare to the stored `event_hash`. A mismatch means the stored bytes diverged from the stored hash (a flipped byte, or a payload edited without re-hashing) — the exact tampering the hash exists to catch.
2. **Sequence integrity (D2):** per-stream `server_sequence` is gapless from 1, has no duplicates, and is monotonic across month files.

Plus, per event: **envelope schema validation** (known types validated against the registry; unknown types get hash + sequence + envelope-shape checks only, no payload validation — D9), and a set of **workspace-level cross-checks** (registry vs. on-disk stream dirs, `body.workspace_id` vs. manifest).

Output is a **human-readable report** (with `--json` for machines) and a **CI-friendly exit code**: `0` clean (or warnings only), `1` findings, `2` usage/IO errors. **All** findings are collected before exit — CI wants the whole picture, not the first failure.

**Blob re-hashing is explicitly out of scope until M4** (no `blobs/` in an M0 workspace). A documented seam is left for it (§4, Ruling 9).

Areas touched: `cli/msgctl/verify.py` (new), one surgical subparser block + one dispatch line in `cli/msgctl/cli.py`, `cli/tests/test_verify*.py` (new). **No `core/` edits, no new runtime deps, no changes to `append.py`/`workspace.py`, nothing in projection/server/web/CI.**

---

## 2. THE crux ruling — recompute over the RAW body, never `verify_hash(envelope)` (D1, non-negotiable)

This is the correctness spine of the ticket and it is already ruled by ENG-56. `hashing.py`'s own docstring states it outright:

> "`msgctl verify` (§11.4) likewise re-hashes the **verbatim stored JSONB dict** via `hash_event`, not `verify_hash`."

And `verify_hash`'s docstring forbids exactly the tempting shortcut:

> "an `Envelope` parsed with `extra="allow"` has already lost those raw bytes, so verifying it here would hash the coerced form and could accept a body whose bytes never matched … The upload validator and `msgctl verify` MUST instead call `hash_event(raw_parsed_body_dict)` on the pre-model parsed JSON."

### Ruling (absolute)

Per stored line, the hash check is:

```python
raw = json.loads(line)                 # raw dict, NEVER Envelope.model_validate
computed = hash_event(raw["body"])     # raw body dict straight to hash_event
if computed != raw["event_hash"]:      # compare against the raw stored hash string
    finding(hash_mismatch, ...)
```

- **`verify_hash(envelope)` is FORBIDDEN for the hash check** and this must be stated in a code comment on the check. `verify_hash` re-dumps `body.model_dump(mode="json")`, and Pydantic lax coercion silently repairs a nonconforming body — `"type_version": "1"` (string) collapses to `1` (int), so `model_dump` emits bytes that differ from what is stored. That normalization would mask **exactly** the divergence verify exists to detect: an attacker (or a bug) that edits `"1"`→`1` in a stored body would pass `verify_hash` and fail the honest raw re-hash. Using `verify_hash` here would make verify a no-op against its own threat model.
- **`hash_event` takes the raw dict.** Its input type is `JSONValue` (a superset of the body dict), and it is a pure `body → "sha256:<hex>"` function that never touches `Body`/`Envelope`. This is the ENG-56-licensed authority path.
- **Compare against the raw stored string** `raw["event_hash"]`, not `envelope.event_hash` — same reason: never launder the stored value through a model.
- **Schema validation is a SEPARATE, additional pass** over the same line (Ruling 5). It uses `Envelope.model_validate` and the payload registry — but its result never feeds the hash check. The two passes are independent by design: the hash pass proves byte-faithfulness; the schema pass proves shape/type validity. Mixing them (e.g. hashing the model dump) is the bug this ruling exists to prevent.
- **`hash_event` may raise `JCSError`** if a stored `body` is out of the JCS domain (non-finite float, over-cap int, lone surrogate, over-depth). Catch it and record a `hash_mismatch` finding with detail `"body not canonicalizable: <err>"` — an un-canonicalizable stored body is by definition a body whose hash cannot be reproduced. Never let `JCSError` escape (it would become an exit-2 traceback).
- **Redaction exemption (§2.1):** if `raw["server"]["payload_redacted"]` is truthy, skip the hash check for that line (a redacted body was intentionally nulled and no longer matches by design), mirroring `verify_hash`'s short-circuit. M0 never redacts, but encoding it keeps verify correct for M4+ and costs one branch.

**Defensive raw reads for sequence/id:** the sequence-contiguity and duplicate-id passes also read from the raw dict (`raw["server"]["server_sequence"]`, `raw["body"]["event_id"]`), not from a validated model — so a line whose *payload* fails schema validation (a real accepted event with a bad-but-known payload) is still counted toward the sequence rather than becoming a phantom gap. Only a line that fails `json.loads` or is not a JSON object, or is missing `server.server_sequence`/`body.event_id`, becomes an `unparseable` hole (Ruling 5).

---

## 3. Design rulings (each ticket question, ruled)

### Ruling 1 — verify has its OWN read loop; it does NOT reuse `append.py`'s scan

`append.py._scan_stream`/`_scan_file` are the wrong discipline for verify and must **not** be reused or refactored (ENG-58 coordination, §8):

| | `append.py` scan | `verify.py` walk |
|---|---|---|
| Torn trailing line | **truncates** the file (repairs) | **reports** it (never mutates disk) |
| First bad line | **raises** `CorruptLogError`, stops | **records a finding, continues** — collect everything |
| Hash | trusts the writer (no re-hash) | **re-hashes every line** (raw) |
| Contiguity break | raises immediately | records `gap`/`duplicate`/`out_of_order`, resyncs, continues |
| Mutates disk | yes (truncate, fsync) | **read-only, always** |

verify therefore has its own `_walk_stream` that opens each month file **read-only** (`read_bytes()`), never truncates, never fsyncs, and never raises on bad data — it turns every anomaly into a `Finding`. It reads `workspace.json` via the existing read-only `Workspace.open` (that's a consume, not a modify, of `workspace.py`).

### Ruling 2 — verify walks `streams/**/*.ndjson` ONLY (the ENG-58 anti-collision rule)

**Pinned rule, stated in a module comment and enforced by the glob:** verify's file discovery is exactly `for sid_dir in (root/"streams").iterdir() if sid_dir.is_dir(): sorted(sid_dir.glob("*.ndjson"))`. It **never** reads anything at the workspace root other than `workspace.json`, and specifically **ignores `projections.sqlite3`** (which ENG-58 will place at the workspace root) and any other non-`streams/` artifact, lock file, or temp file. This is the hard boundary that lets ENG-58 add a projection DB at the root without ENG-60 ever tripping over it. A `.ndjson` file directly under `streams/` (not inside a `<stream_id>/` subdir) is ignored with a verbose-only note (it is not a stream log).

### Ruling 3 — Torn trailing line = **warning, not failure** (exit 0 if all *terminated* lines are valid)

A torn trailing line is an **interrupted write, not corruption** — `send` writes line+`\n`+fsync before acknowledging, so a partial trailing line was never acked and its would-be sequence is unconsumed (append reuses it on the next send). verify must not punish an honest interrupted write.

- **Definition:** a month file whose bytes are non-empty and do **not** end in `\n`. The final chunk (bytes after the last `\n`) is the torn line.
- **Ruling:** emit a `torn_line` finding at **severity `warning`**. Do **not** hash/sequence/schema-check the partial chunk (it is incomplete). **Continue** verifying all terminated lines. If everything terminated is valid and this is the only class present, **exit 0** — a torn trailing line alone is a clean verify.
- verify **never truncates** it (that is `append`'s job on the next write; verify is read-only, Ruling 1).
- **Placement nuance:** normally only the lexically-last month file of a stream can carry a torn line. A torn (unterminated) trailing chunk on a **non-last** month file is still emitted as `torn_line` warning (verify does not assume; it reports what it sees) but the plan notes it as suspicious in the detail string. It does not escalate to failure — escalating would make verify fail on a benign interrupted write that happened to be followed by a later month file, which is legitimate if the clock advanced a month between the torn write and the repair-append.

### Ruling 4 — Findings taxonomy (severity per class)

`Finding(severity, cls, stream_id, sequence, event_id, file, detail)`. `sequence`/`event_id` are `None` when unknown (e.g. an `unparseable` line).

| class | severity | meaning |
|---|---|---|
| `hash_mismatch` | **failure** | recomputed `hash_event(raw body)` ≠ stored `event_hash` (flipped body byte / edited payload w/o re-hash / un-canonicalizable body) |
| `gap` | **failure** | missing sequence number(s): next seq > expected |
| `duplicate` | **failure** | same `server_sequence` value appears twice in a stream |
| `out_of_order` | **failure** | a line's seq is < a preceding line's seq (non-monotonic on disk), not otherwise a duplicate |
| `duplicate_event_id` | **failure** | same `event_id` at two **different** sequences (idempotency-violating on disk; append prevents it, verify checks the disk) |
| `schema_invalid` | **failure** | envelope-shape-valid, type is **known** in the registry, but payload fails its registry model |
| `unparseable` | **failure** | terminated line fails `json.loads`, is not a JSON object, fails `Envelope.model_validate`, or lacks `server.server_sequence`/`body.event_id` |
| `unregistered_stream_dir` | **failure** | a `streams/<sid>/` directory (with logs) has no entry in `workspace.json` — data outside the registry |
| `workspace_id_mismatch` | **failure** | `body.workspace_id` ≠ the manifest `workspace_id` |
| `manifest_invalid` | **failure** | `workspace.json` is malformed or has a duplicate stream name (see Ruling 6) |
| `torn_line` | **warning** | unterminated trailing bytes — interrupted write (Ruling 3) |
| `empty_registered_stream` | **warning** | registry entry whose stream dir is missing or contains zero events (Ruling 6) |

- **Unknown event type is NOT a finding.** `get_payload_model(type, type_version) is None` → payload validation is **skipped** (D9); the event still receives hash + sequence + envelope-shape checks. Log it at **verbose only** (`--verbose`), never as a finding. `message.created` v1 → full payload validation via `MessageCreatedV1`.
- **Exit code = max severity present:** any `failure` → **1**; only `warning`(s) → **0**; clean → **0**. Usage/IO (not a workspace, unreadable dir, argparse) → **2**.

### Ruling 5 — Per-line algorithm (two independent passes)

For each terminated line in a stream (month files ascending, lines in file order):

**Pass A — parse + hash (raw, Ruling 2):**
1. `raw = json.loads(line)`; if it raises or `raw` is not a `dict` → `unparseable`, treat as a hole (do not advance the expected sequence), continue.
2. Read `seq = raw["server"]["server_sequence"]` and `eid = raw["body"]["event_id"]` defensively (KeyError/TypeError → `unparseable` hole, continue).
3. Hash: if not redacted, `computed = hash_event(raw["body"])` (catch `JCSError`); compare to `raw["event_hash"]` → `hash_mismatch` on mismatch or JCS failure.

**Pass B — sequence + id bookkeeping** (using `seq`, `eid` from A):
- Maintain `expected` (starts 1), `seen_seqs: set[int]`, `seen_ids: dict[str, int]` (event_id → first seq).
- `seq == expected` → advance `expected = seq + 1`.
- `seq in seen_seqs` → `duplicate` (do not advance).
- `seq > expected` → `gap` (detail: `missing {expected}..{seq-1}`); resync `expected = seq + 1` so one gap doesn't cascade into a finding per subsequent line.
- `seq < expected` and not in `seen_seqs` → `out_of_order`.
- record `seq` in `seen_seqs`; if `eid` already in `seen_ids` at a **different** seq → `duplicate_event_id`; else record.

**Pass C — schema validation** (independent of A/B, additive):
- `env = Envelope.model_validate(raw)`; `ValidationError` → `unparseable` (envelope shape is broken).
- `model = get_payload_model(env.body.type, env.body.type_version)`.
  - `None` → unknown type: **skip** payload validation (D9), verbose-log, no finding.
  - else `model.model_validate(env.body.payload)`; `ValidationError` → `schema_invalid` (detail = the validation error, truncated).

A stream that is empty (no terminated lines, no torn line) contributes no findings from A–C; it is handled by the registry cross-check (Ruling 6).

### Ruling 6 — workspace.json cross-checks

- **Registry entry, missing/empty stream dir** → `empty_registered_stream` (**warning**). A registered channel that was created but never sent to is legitimate; not a failure.
- **Stream dir with logs, no registry entry** → `unregistered_stream_dir` (**failure**). Data that no manifest knows about is a real integrity problem (rename-safe id keying means an orphan dir is unreachable).
- **`body.workspace_id` ≠ manifest `workspace_id`** → `workspace_id_mismatch` (**failure**) — a body claiming a different workspace is corruption/misfile. Checked once per line in Pass A (cheap string compare against the manifest value).
- **Manifest itself:** verify opens `workspace.json` via `Workspace.open`. `WorkspaceError` (no `workspace.json`) → **exit 2** (not a workspace — a usage error, not a finding). `CorruptLogError` (malformed manifest or duplicate stream name) → emit one `manifest_invalid` **failure** finding and **continue walking** `streams/` in best-effort mode with an empty registry: in that mode, **suppress `unregistered_stream_dir` and `workspace_id_mismatch`** (the registry/workspace_id are unknown, so those checks would be noise), but still run all per-line hash/sequence/schema checks. This keeps verify useful on a workspace whose manifest is damaged.

### Ruling 7 — `--json` machine-readable output → **YES** (cheap now; CI + ENG-61/62 will want it)

Ship `--json` in this ticket. It is a few lines, and downstream verify-consuming tickets (CI gate, and any ENG-61/62 tooling) benefit from a stable schema rather than scraping human text. Shape:

```json
{
  "root": "/abs/path",
  "workspace_id": "w_01JZ…",
  "ok": false,
  "summary": {"streams": 2, "events": 131, "failures": 1, "warnings": 0, "findings_total": 1},
  "streams": [
    {"stream_id": "s_01…", "name": "general", "events": 128,
     "first_seq": 1, "last_seq": 128, "failures": 0, "warnings": 0}
  ],
  "findings": [
    {"severity": "failure", "class": "hash_mismatch", "stream_id": "s_02…",
     "sequence": 2, "event_id": "01J…", "file": "streams/s_02…/2026-07.ndjson",
     "detail": "recomputed sha256:… != stored sha256:…"}
  ]
}
```

- `ok` = (no failures). `file` paths are **relative to the workspace root** (stable across machines, CI-diffable).
- **The `--json` finding list is NOT capped** (machine output). The human output IS capped (Ruling 8). One JSON object to stdout; nothing else on stdout in `--json` mode (warnings/errors still go to stderr). Exit code is identical in both modes.

### Ruling 8 — Collect ALL findings; cap only the human display

- verify **never stops at the first finding** — CI wants the full picture. It walks every stream, every month, every line, accumulates all findings, then prints and exits.
- **Human output cap: 100 detail lines.** If more than 100 findings, print the first 100 (failures before warnings, then by stream/seq) and a `… +N more findings (use --json for the full list)` line. **Counts in the summary are always complete** (uncapped). `--json` is uncapped. Cap constant `MAX_HUMAN_FINDINGS = 100`.

### Ruling 9 — Blob re-hashing seam (out of scope until M4)

An M0 workspace has no `blobs/` tree (ENG-57 §9 subset omits it). Leave a **documented seam**, not a stub that runs:
- A module-level docstring note and a single `# M4 SEAM:` comment marking where a `_verify_blobs(root, report)` pass would slot into `verify_workspace` (after the stream walk, before the summary).
- `file_ids` referenced inside `message.created` payloads are **not** cross-checked against blob existence in M0 (no blob store) — noted in Risks. The seam is the contract: M4's blob ticket adds the pass and its own findings classes (`blob_missing`, `blob_hash_mismatch`) without touching the stream-walk code.

### Ruling 10 — CLI surface (surgical, one subparser block + one dispatch line)

`verify` is added to the existing argparse in `cli.py` exactly like `init`/`send` — **one `subparsers.add_parser("verify", …)` block + `set_defaults(handler=cmd_verify)`**, and `cmd_verify` is a thin adapter that calls `verify.verify_workspace(...)` and returns its exit code. No change to `main`'s dispatch/`try-except` structure, no change to `init`/`send`. Signature:

```
msgctl verify <dir> [--json] [--verbose]
```

- `<dir>` positional (matches `init`/`send`).
- `--json` (Ruling 7), `--verbose` (surfaces unknown-type notes and per-stream OK lines).
- Exit codes via a small `MsgctlError` subtype for the usage/IO path (exit 2) — see §4. Findings-based exit (0/1) is returned directly by `cmd_verify` (it is not an exception; a workspace with findings is a *successful run that found problems*).

---

## 4. File list

**Create:**

| File | Purpose |
|---|---|
| `cli/msgctl/verify.py` | The whole verifier: `Severity` enum, `Finding` dataclass, `StreamSummary`, `VerifyReport`; `_walk_stream` (read-only, own loop — Ruling 1); the raw-hash check (Ruling 2), sequence/id bookkeeping (Ruling 5B), schema pass (Ruling 5C); `_cross_check_registry` (Ruling 6); `verify_workspace(root, *, verbose) -> VerifyReport`; `format_human(report, cap)` and `format_json(report)`; `report.exit_code`. Contains the `# M4 SEAM:` blob note (Ruling 9). |

**Modify (surgical only):**

| File | Change |
|---|---|
| `cli/msgctl/cli.py` | Add the `verify` subparser block + `cmd_verify` handler (Ruling 10). `cmd_verify`: `report = verify.verify_workspace(args.dir, verbose=args.verbose)`; print `format_json` or `format_human`; `return report.exit_code`. Import `from msgctl import verify`. Nothing else changes. |
| `cli/msgctl/errors.py` | *(optional, small)* add `UsageError(MsgctlError)` with `exit_code = 2` for the "not a workspace / unreadable path" case so `main`'s existing `except MsgctlError` maps it to exit 2. If preferred, reuse argparse's exit-2 by validating in the handler and raising this. |

**Create (tests, `cli/tests/`):** `test_verify_green.py`, `test_verify_corruption.py`, `test_verify_torn.py`, `test_verify_schema.py`, `test_verify_registry.py`, `test_verify_json.py` (or a single `test_verify.py` with classes — implementer's call; §6 lists cases). Reuse `conftest.py` helpers (`run_cli`, `read_lines`, `only_stream_dir`).

**Explicitly NOT touched (ENG-58 collision boundary, §8):** `cli/msgctl/append.py`, `cli/msgctl/workspace.py` (read-only consume of `Workspace.open` only), anything projection-related, all of `server/`, `cli/pyproject.toml`, root `pyproject.toml`, `uv.lock`, CI.

**No new dependencies:** stdlib (`json`, `enum`, `dataclasses`, `pathlib`) + `msgd.core` (`hash_event`, `Envelope`, `get_payload_model`) + `msgctl.workspace.Workspace`. Confirmed against `cli/pyproject.toml` (`dependencies = ["msgd"]`).

---

## 5. Step-by-step (all `python-engineer`)

**Step 1 — `verify.py` scaffolding.** `Severity(Enum)` = `FAILURE`/`WARNING`; `Finding` frozen dataclass (severity, cls, stream_id, sequence: int|None, event_id: str|None, file: str, detail: str); `StreamSummary` (stream_id, name, events, first_seq, last_seq, failures, warnings); `VerifyReport` (root, workspace_id, findings: list[Finding], streams: list[StreamSummary]) with a computed `exit_code` property (`1` if any FAILURE, else `0`) and `ok`/counts helpers. `MAX_HUMAN_FINDINGS = 100`.

**Step 2 — `_walk_stream(stream_dir, stream_id, manifest_wsid, findings)`.** Own read-only loop (Ruling 1): for each `sorted(stream_dir.glob("*.ndjson"))`, `raw_bytes = path.read_bytes()`; detect torn trailing (non-empty, no final `\n`) → `torn_line` warning, drop the partial chunk from checking (Ruling 3); iterate terminated lines running Passes A/B/C (Ruling 5), appending findings; carry `expected`/`seen_seqs`/`seen_ids` **across month files** (contiguity spans months, ascending lexical = chronological, matching append's scan semantics). The raw-hash comment (`# RAW body → hash_event; verify_hash is FORBIDDEN here — it would mask coercion tampering`) lives on Pass A.

**Step 3 — `_cross_check_registry(root, ws, findings)`** (Ruling 6): registry entries with missing/empty dirs → `empty_registered_stream` (warning); `streams/<sid>/` dirs with logs but no registry entry → `unregistered_stream_dir` (failure). (`workspace_id_mismatch` is checked inline in Pass A against `ws.workspace_id`.)

**Step 4 — `verify_workspace(root, *, verbose)`.** `Workspace.open(root)` (WorkspaceError → raise `UsageError` exit 2; CorruptLogError → `manifest_invalid` finding + best-effort empty-registry mode, Ruling 6). Walk each registered + orphan stream dir via `_walk_stream`; run `_cross_check_registry`; build per-stream summaries; `# M4 SEAM: _verify_blobs(root, report)` (Ruling 9); return the `VerifyReport`.

**Step 5 — formatters.** `format_human(report, cap=MAX_HUMAN_FINDINGS)`: per-stream summary lines (`stream s_… (general): 128 events, seq 1..128, OK` / `… N findings`), then findings (failures first, capped with `… +N more`), then a totals line (`131 events across 2 streams: 1 failure, 0 warnings`). `format_json(report)` per Ruling 7 (uncapped). `--verbose` adds unknown-type notes + OK stream lines.

**Step 6 — `cli.py` wiring** (Ruling 10): subparser block, `cmd_verify` adapter, `from msgctl import verify` import. `errors.py`: add `UsageError(exit_code=2)` if not reusing argparse.

**Step 7 — local gates.** `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`; then by hand: `msgctl init` + a few `send`s → `msgctl verify` prints green exit 0; flip a byte → exit 1.

---

## 6. Test plan (`cli/tests/`, `tmp_path`) — every AC as an explicit test

**Build the fixture with REAL `msgctl` sends via subprocess** (`run_cli` from `conftest`), then craft each corruption by **direct file manipulation** on the resulting month file — this proves verify catches divergence in genuinely-produced logs, not hand-built straw men.

- **`test_verify_green` (AC: clean workspace verifies green):** init + N sends across ≥2 streams (incl. an auto-created second stream, and force a second month file if easy via a crafted `server_received_at`) → `verify` exit **0**, zero findings, summary counts match N, `ok` true. Also `verify` on a freshly-`init`'d workspace with **zero** streams → exit 0.
- **`test_verify_flipped_body_byte` (AC corruption #1):** send; flip one byte inside a stored `body` (e.g. a char of `text`) leaving the line valid JSON and terminated → exactly one `hash_mismatch` finding with the right `stream_id`/`sequence`/`event_id`, exit **1**. This is the case that would **pass** if verify used `verify_hash` on a re-parsed model in some cases — assert the raw path catches it.
- **`test_verify_edited_payload_without_rehash` (AC corruption #4, distinct from a dup line):** parse a stored line, mutate a `payload` field (e.g. `text`) but keep the original `event_hash`, rewrite the line → one `hash_mismatch`, exit 1. Explicitly a **different** corruption from a duplicated line: here the hash no longer matches the (edited) body.
- **`test_verify_coercion_tamper_is_caught` (the crux regression, pins Ruling 2):** take a stored line, edit `body.type_version` from `1` to `"1"` (string) **without** changing `event_hash`, rewrite → verify reports `hash_mismatch` (the raw JCS of `"1"` differs from `1`). Assert this is a **failure**, exit 1 — the direct proof that verify does **not** launder through `model_dump`, which would coerce `"1"`→`1` and mask it. (Guard-rail test that fails loudly if anyone swaps in `verify_hash`.)
- **`test_verify_deleted_line_is_gap` (AC corruption #2):** send ≥3; delete a middle line → one `gap` finding (detail names the missing seq), exit 1. Assert a single gap does not cascade (resync), and the following lines are not each reported.
- **`test_verify_duplicated_line_is_duplicate` (AC corruption #3):** send ≥2; duplicate one whole line (same bytes, same seq) → `duplicate` (and, since same bytes ⇒ same event_id at the same seq, NOT `duplicate_event_id` — assert that class is absent, distinguishing it from the edited-payload case), exit 1.
- **`test_verify_torn_trailing_is_warning` (torn policy, Ruling 3):** send a few; append a partial (no trailing `\n`) chunk to the last month file → one `torn_line` **warning**, **exit 0**, and the terminated lines still verify; assert verify **did not truncate** the file (byte-identical before/after — read-only).
- **`test_verify_unparseable_terminated_line` (Ruling 5):** append a terminated bad-JSON line and a terminated valid-JSON-non-envelope line → two `unparseable` findings, exit 1, file untouched.
- **`test_verify_unknown_type_not_a_finding` (D9):** hand-write a terminated unknown-`type` envelope (correct raw `event_hash = hash_event(body)`, correct next seq) → verify **exit 0**, zero findings, and `--verbose` mentions the unknown type; assert its hash + sequence were still checked (bump seq by giving it the right number; a wrong seq would produce a gap — prove the sequence pass ran).
- **`test_verify_schema_invalid_known_type` (Ruling 5C):** craft a `message.created` v1 line whose payload violates `MessageCreatedV1` (e.g. `format: "html"` or a malformed `message_id`) but with a correct raw `event_hash` over that bad body → one `schema_invalid` finding (NOT `hash_mismatch` — the hash is faithful to the bad body), exit 1. Proves the schema pass is independent of the hash pass.
- **`test_verify_unregistered_stream_dir` (Ruling 6):** create a `streams/<fake_sid>/2026-07.ndjson` with a real-looking line but no manifest entry → `unregistered_stream_dir` failure, exit 1.
- **`test_verify_empty_registered_stream` (Ruling 6):** register a stream (via a send then… actually) — create a registry entry with an empty/missing dir → `empty_registered_stream` **warning**, exit 0.
- **`test_verify_workspace_id_mismatch` (Ruling 6):** edit a stored `body.workspace_id` to a different valid `w_` id **and** fix the `event_hash` to match (so it's not also a hash finding) → `workspace_id_mismatch` failure, exit 1.
- **`test_verify_json_shape` (Ruling 7):** run `verify --json` on a corrupted workspace → stdout is a single JSON object with the documented keys (`ok`, `summary`, `streams`, `findings` with relative `file` paths); exit code matches the human run; nothing but JSON on stdout.
- **`test_verify_exit_codes`:** parametrized — clean → 0; warning-only → 0; any failure → 1; `verify` on a non-workspace dir → 2; `verify` on a missing dir → 2.
- **`test_verify_collects_all_findings` (Ruling 8):** inject three distinct failures in one workspace → all three present in one run (verify did not stop at the first); and (optionally) inject >100 to assert the human `+N more` cap while summary counts stay complete and `--json` is uncapped.

Map to ACs: clean-green → `test_verify_green`; flipped byte → `test_verify_flipped_body_byte`; deleted line/gap → `test_verify_deleted_line_is_gap`; duplicated line → `test_verify_duplicated_line_is_duplicate`; edited payload w/o re-hash → `test_verify_edited_payload_without_rehash`; CI exit codes → `test_verify_exit_codes`. The coercion-tamper test is the extra guard-rail that pins the crux ruling.

---

## 7. Risks / open questions

- **The `verify_hash` trap (highest risk).** A future edit could "simplify" the hash check to `verify_hash(envelope)` and silently neuter verify against coercion tampering. Mitigated by: the bold code comment on Pass A, and `test_verify_coercion_tamper_is_caught` which fails the moment anyone makes that swap. Called out here so review round 1 checks the raw path survived.
- **`JCSError` on a stored body.** A stored body that is out of the JCS domain cannot be re-hashed; ruled to surface as `hash_mismatch` (detail explains). Alternative (a dedicated `uncanonicalizable` class) rejected as taxonomy bloat — the user-facing meaning is "this stored hash can't be reproduced," which *is* a hash mismatch. Noted so a reviewer doesn't expect a separate class.
- **Best-effort mode on a corrupt manifest** (Ruling 6) suppresses registry/workspace_id cross-checks. Documented trade-off: verify stays useful on a damaged manifest rather than emitting noise, at the cost of not detecting orphan dirs when the registry itself is unreadable. Acceptable — a `manifest_invalid` failure already fails the run.
- **No blob/file_id existence check in M0** (Ruling 9). `message.created` `file_ids` are format-validated by `MessageCreatedV1` but not checked against a blob store (there is none). The `# M4 SEAM:` marks where M4 adds it. Flagged for the M4 export/blob ticket.
- **Month-boundary contiguity.** Sequence bookkeeping must span month files. `_walk_stream` carries `expected`/`seen_*` across the ascending-sorted month files (same lexical=chronological property `append` relies on). Recommended: the green test forces two month files so cross-file contiguity is exercised, not just asserted.
- **Cost.** O(total lines) with one JCS+SHA-256 per line — the honest cost of an independent re-hash and exactly the point of verify. Fine at M0 scale; if a large-workspace M4 verify needs it, a `--sample`/parallelism flag is a future addition (noted, not built).
- **Windows.** verify is pure stdlib + `pathlib`, no `fcntl` — so unlike `send` it is POSIX-independent. No lock is taken (read-only). If a `send` races a `verify`, verify may observe a torn trailing line mid-write → reported as a `torn_line` warning, which is correct and harmless. Documented: verify is intentionally lock-free and read-only.

---

## 8. Collision protocol with ENG-58 (SQLite projection, parallel)

Pinned so the two tickets **cannot** touch the same code:

- **ENG-60 OWNS:** `cli/msgctl/verify.py` (new), the `verify` subparser block + `cmd_verify` handler in `cli.py` (one add-parser block + one dispatch line — no change to `init`/`send`/`main`), an optional `UsageError` in `errors.py`, and `cli/tests/test_verify*.py`.
- **ENG-60 does NOT own / must not modify:** `append.py`, `workspace.py` (consumed read-only via `Workspace.open` — no refactor), and **anything projection-related**. verify has its **own** read loop (Ruling 1) precisely so it never needs to touch append's scan.
- **The `streams/**/*.ndjson`-only rule (Ruling 2)** is the hard boundary: verify reads `workspace.json` + `streams/<sid>/*.ndjson` and **nothing else** at the root. ENG-58's `projections.sqlite3` (and any WAL/shm sidecars) at the workspace root are **ignored** by verify's walk. Neither ticket reads the other's file.
- Both tickets add exactly one subparser to `cli.py`. To avoid a merge conflict on that one file, whichever lands second rebases its single `add_parser` block on top of the first — a trivial, non-semantic conflict at worst. Flag in both session files so the second implementer expects it.

---

## 9. Acceptance-criteria mapping

| AC | Covered by |
|---|---|
| Recompute `event_hash` for every event, report mismatches w/ stream+seq+event_id | Ruling 2, Pass A, `test_verify_flipped_body_byte` / `_edited_payload` / `_coercion_tamper` |
| Per-stream contiguity: gapless from 1, no dup, monotonic | Ruling 5B, `test_verify_deleted_line_is_gap` / `_duplicated_line` |
| Envelope schema validation (known types only; unknown = hash+seq only, D9) | Ruling 5C, `test_verify_schema_invalid_known_type` / `_unknown_type_not_a_finding` |
| Human-readable report + non-zero exit on failure (CI) | Rulings 4/8/10, `format_human`, `test_verify_exit_codes` |
| Blob re-hashing out of scope, documented seam | Ruling 9 (`# M4 SEAM:`) |
| Clean workspace verifies green | `test_verify_green` |
| Each corruption class detected (flipped byte / gap / dup line / edited payload) | the four mandated tests in §6 |
| CI-suitable exit codes (0/1/2) | Ruling 4, `test_verify_exit_codes` |
| `--json` machine output | Ruling 7, `test_verify_json_shape` |
| Raw-hash discipline (never `verify_hash`) | Ruling 2 + `test_verify_coercion_tamper_is_caught` |

---

## Review Round 1 — Triage & Fix Plan

Reviewer verdict: REQUEST_CHANGES (comment form, own-PR) on PR #7 — 2 blocking + 3 nits. The
reviewer confirms the core is right (raw-hash discipline verified end-to-end, read-only guarantee,
ENG-58 boundary, contiguity taxonomy). I verified both blocking findings against the branch and
`workspace.py`: both are real. **All five ADDRESSED** (finding 4 is addressed as *documentation
only* — the current behavior is ruled correct). Every fix is small and lands in one fixup commit.
Implementer: `python-engineer`. Scope discipline: fixes are confined to `verify.py` +
`cli/tests/test_verify*.py` — `workspace.py` stays untouched (ENG-58 partition, plan §8).

**Summary of dispositions**

| # | Finding | Severity | Decision |
|---|---|---|---|
| 1 | Uncaught `KeyError` on valid-JSON manifest missing `workspace_id` | blocking | **ADDRESS — broaden the best-effort except tuple in `verify.py`; two-case test** |
| 2 | `out_of_order` detector branch untested | blocking | **ADDRESS — seq `1,3,2` test** |
| 3 | Hash pass on tampered unknown-type line untested | nit | **ADDRESS — one cheap test** |
| 4 | Mid-walk `OSError` → exit-2 abort vs finding+continue | nit | **ADDRESS as documentation — exit 2 is RULED correct for M0, no behavior change** |
| 5 | Gap-at-start untested | nit | **ADDRESS — parametrize the existing gap test** |

### Finding 1 — Uncaught `KeyError` on malformed manifest — ADDRESS (blocking, fix in `verify.py` only)

**Verified real.** `verify_workspace` catches only `WorkspaceError` (→ `UsageError`, exit 2) and
`CorruptLogError` (→ `manifest_invalid` + best-effort). But `Workspace.open` performs three
untrusted reads *outside* its own internal try, so a valid-JSON manifest can raise bare exceptions
that sail past `main`'s `except MsgctlError` into a traceback — exactly the corrupt-manifest class
verify exists to survive (plan Rulings 1/6/8). The full leak surface (confirmed by reading
`workspace.py:155–196`):

- `raw["workspace_id"]` → **KeyError** (the reviewer's empirical repro);
- `info["name"]` per stream entry → **KeyError** (entry missing `name`) or **TypeError** (entry is
  a string/list, not a dict);
- `streams_raw.items()` → **AttributeError** when `"streams"` is a list/string (the internal try
  annotates `dict[str, Any]` but never runtime-checks it).

**Fix (`cli/msgctl/verify.py` `verify_workspace`, the `Workspace.open` try only — `workspace.py`
is off-limits per the ENG-58 partition and the plan's file list):** broaden the best-effort branch
to the exact tuple

```python
except WorkspaceError as exc:
    raise UsageError(str(exc)) from exc
except (CorruptLogError, KeyError, TypeError, ValueError, AttributeError) as exc:
    findings.append(
        Finding(Severity.FAILURE, "manifest_invalid", None, None, None,
                "workspace.json", f"malformed manifest: {exc!r}")
    )
    ws = None
```

Rulings inside the fix:
- **`WorkspaceError` stays FIRST and separate** — "no `workspace.json` at all" remains exit 2
  (not-a-workspace is a usage error, not a finding). Order matters; do not fold it into the tuple.
- **Exact tuple `(CorruptLogError, KeyError, TypeError, ValueError, AttributeError)`**, scoped to
  this one `try` around `Workspace.open` and nothing else. KeyError/TypeError/AttributeError cover
  the three verified leak paths; ValueError is included (reviewer's suggestion) as the standard
  malformed-scalar parse error so a future `workspace.py` tweak doesn't reopen the hole.
  **Bare `except Exception` is REJECTED**: it would relabel genuine verify bugs
  (NameError, RecursionError, etc.) as `manifest_invalid` findings and hide crashes CI must see.
  The tuple is the honest middle.
- **Use `{exc!r}` in the detail, not `{exc}`** — `str(KeyError('workspace_id'))` is just
  `'workspace_id'`, which is useless in a report; the repr names the exception class.
- Downstream best-effort behavior is already correct (`ws = None` → registry/`workspace_id`
  cross-checks suppressed, per-line hash/sequence/schema checks still run); no other change.

**Test (`test_verify_registry.py` or wherever `test_verify_manifest_invalid_best_effort` lives):**
parametrize a new `test_verify_manifest_malformed_shapes_best_effort` with two cases, each built on
a real workspace (init + one send, then edit `workspace.json`):
1. **delete the `workspace_id` key** (the KeyError repro) — keep everything else intact;
2. **set `"streams": []`** (the AttributeError path — same fix covers it, one param proves the
   tuple isn't KeyError-only).

For each: `verify` → exit **1** (no traceback — assert `"Traceback" not in stderr`), exactly one
`manifest_invalid` finding, **the stream walk still ran** (summary shows the 1 event; its hash
check executed — assert no `hash_mismatch`, i.e. events were visited and clean), no
`unregistered_stream_dir`/`workspace_id_mismatch` noise (suppressed in best-effort mode), and
`--json` emits a well-formed object with `ok: false`. The existing JSONDecodeError-path test stays
as-is.

### Finding 2 — `out_of_order` untested — ADDRESS (blocking)

Correct: the branch exists (verified at `verify.py` Pass B: `else: # seq < expected and not
previously seen`), it's a first-class failure class in Ruling 4, and no test reaches it. For an
integrity tool, an untested detector is an unproven detector.

**Test (`test_verify_corruption.py`) — `test_verify_out_of_order`:** init + 3 real sends (seqs
1,2,3); rewrite the month file with the same three **unmodified** lines in file order **1, 3, 2**
(lines untouched ⇒ hashes stay faithful, so the only findings are sequence findings). Expected
walk: seq 1 ok (expected→2); seq 3 > 2 → **`gap`** ("missing 2..2", resync expected→4); seq 2 < 4
and not in `seen_seqs` → **`out_of_order`**. Assert: exit 1; finding classes are exactly
`{gap, out_of_order}` (one each — the gap/resync interplay is intended and now pinned);
the `out_of_order` finding carries `sequence == 2` and the correct `event_id`; **no**
`duplicate` and **no** `hash_mismatch`. This also documents the accepted semantics: a
late-arriving sequence is reported as *both* the hole it left and the out-of-place line — two
true statements about the disk, not double-counting.

### Finding 3 — Hash pass on tampered unknown-type line untested — ADDRESS (nit, cheap)

The code is correct (Pass A hashes every line before the D9 skip in Pass C), but the false-negative
audit deserves the pin: unknown types must never become a hashing blind spot.

**Test (`test_verify_schema.py`, next to `test_verify_unknown_type_not_a_finding`) —
`test_verify_unknown_type_tampered_hash_is_caught`:** reuse the existing unknown-type line crafting
(`type="widget.exploded"`, correct next seq) but set `event_hash` to a syntactically valid, wrong
digest (e.g. `"sha256:" + "0"*64`). Assert: exit 1, exactly one `hash_mismatch` finding (correct
stream/seq/event_id), and **no** `schema_invalid`/`unparseable` (the D9 skip still applies —
payload validation stayed off; only the hash fired).

### Finding 4 — Mid-walk `OSError` → exit 2 — ADDRESS as documentation; behavior RULED correct

**Ruling: keep exit-2 abort for M0.** A mid-walk `OSError` (permission denied, disk error,
vanished file) is **environmental, not log corruption** — it says nothing about the integrity of
the bytes verify exists to judge. Converting it to a finding + continue would be worse on both
axes: exit 1 would misreport an ops problem as data corruption, and any "partial verify" that
skips an unreadable month file **cannot honestly claim the stream is gapless** — a green-ish
report over an incompletely-read log is precisely the false assurance a verifier must never emit.
Exit 2 already means "the run itself could not complete" in our convention (plan Ruling 4:
usage/IO), and CI treats it as infra-red, not corruption-red. This also matches the plan's
existing `UsageError` mapping for an unreadable `streams/` dir — the mid-walk case is the same
class.

**Change (doc only):** one sentence in `verify_workspace`'s docstring and a line in the plan's
Risks: "A mid-walk `OSError` on a month file aborts the run with exit 2 (environmental, not a
finding): a partially-read stream cannot honestly be reported gapless. Revisit at M4 scale if
long-running verifies want per-stream isolation." No test (simulating a mid-walk `OSError`
portably means chmod tricks that are flaky as root/CI; the abort path is a two-line re-raise
already exercised by the unreadable-dir case).

### Finding 5 — Gap-at-start untested — ADDRESS (nit, ~4 lines)

Same code branch as the middle gap (`seq > expected` with `expected == 1`), but boundary pins are
cheap and the first-line case is the one a chopped-head file produces in the wild.

**Fix:** parametrize the existing `test_verify_deleted_line_is_gap` (delete middle → `missing
2..2` vs delete **first** → `missing 1..1`), asserting one `gap` finding with the right detail and
exit 1 in both cases. No new file.

### Net change scope (one fixup commit)

- `cli/msgctl/verify.py` — broaden the `Workspace.open` except tuple + `{exc!r}` detail (F1);
  one docstring sentence on mid-walk `OSError` (F4). **No other code change; `workspace.py`
  untouched (ENG-58 partition holds).**
- `cli/tests/` — `test_verify_manifest_malformed_shapes_best_effort` (2 params, F1);
  `test_verify_out_of_order` (F2); `test_verify_unknown_type_tampered_hash_is_caught` (F3);
  parametrize gap test for first-line deletion (F5).
- This session file — Risks note for the F4 ruling (done above).

No plan rulings overturned — Rulings 1–10 stand; F1 tightens Ruling 6's best-effort promise to
cover every malformed-manifest shape `Workspace.open` can actually raise, F4 formalizes what
Ruling 4's exit-2 class already implied. Re-run local gates (`ruff check`, `ruff format --check`,
`mypy`, `pytest`) before pushing; reply on the five review threads with these dispositions.
