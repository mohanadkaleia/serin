# ENG-61 — M0: Rebuild ≡ incremental equivalence test in CI (permanent gate)

Milestone M0. TDD §5 ("CI runs it and diffs against incremental state — M0 exit
criterion, kept forever"); seed of the §12 invariant 6. Depends on ENG-58
(`project`/`dump_messages`), ENG-59 (`rebuild_projection` / `msgctl rebuild`,
PR #9 approved), ENG-60 (`verify`). All prerequisites merged/merging; this ticket
is **test + CI only** — zero production-code changes.

## Goal (restated)

Ship a **property-based** test that, for randomized send sequences across multiple
streams, proves the three permanent projection promises hold end-to-end and wire it
into CI as a **required, permanent** check:

1. **rebuild ≡ incremental** — an incrementally-built projection and a full
   `rebuild_projection` of the same log produce a **byte-identical**
   `dump_messages`.
2. **verify green** — `msgctl verify` exits 0 on the generated workspace.
3. **idempotence** — re-`project`-ing applies 0 rows and leaves the dump unchanged.

Plus a **mutation/teeth test** proving the equality comparison actually catches a
one-row divergence, and **determinism** under a fixed hypothesis seed in CI.

This is the M0 exit criterion and a **permanent gate** — never deleted, extended at
M1 (server projections) and M2 (client Dexie).

## What already exists (grounding)

- `cli/msgctl/projection.py` — `open_db`, `project(ws, conn) -> ProjectResult`,
  `dump_messages(conn) -> str` (the **pinned contract**: fixed `_DUMP_COLUMNS`
  order, `ORDER BY stream_id, server_sequence`, compact `separators=(",",":")`,
  `ensure_ascii=False`, `\n`-joined). Unknown `(type, type_version)` → skipped,
  cursor still advances (D9).
- `cli/msgctl/rebuild.py` (ENG-59) — `rebuild_projection(ws) -> ProjectResult`:
  full replay into a fresh temp DB, atomic `os.replace` swap over the live
  `projections.sqlite3`. `msgctl rebuild <dir>` is its CLI form.
- `cli/msgctl/append.py` — `append_event(ws, stream_id, *, build_envelope)`:
  the in-process sequencer. `build_envelope(next_seq, recv_at)` is called **inside
  the per-stream lock** and MUST return a fully-formed `Envelope` with
  `server=ServerMetadata(server_sequence=next_seq, server_received_at=recv_at, …)`
  (exactly as `cmd_send` does). This is the fast in-process send path.
- `cli/msgctl/workspace.py` — `init_workspace`, `Workspace.open`,
  `resolve_or_create_stream(ws, name)` (mutates `ws.streams` in place so a later
  `project(ws, conn)` sees new streams), `now_rfc3339`.
- `cli/msgctl/verify.py` — `verify.verify_workspace(root) -> VerifyReport`;
  `report.exit_code` is 0 when clean. Unknown types are D9 notes, **not** findings,
  so injected `widget.exploded` events keep verify green. M0 has **no** blobs/
  `file_ids` cross-check (M4 seam) — random valid `f_`/`u_`/`m_` ids in payloads do
  not fail verify.
- `msgd.core`: `build_message_created_body(...)`, `Body`, `Envelope`,
  `ServerMetadata`, `hash_event`, `ids.new_*`.
- **Deps already in place**: root `pyproject.toml` dev group already has
  `hypothesis>=6.100`; `testpaths = ["server/tests", "cli/tests"]`, so a new file
  under `cli/tests/` runs in the existing `Pytest` CI step automatically. **No
  dependency or config change required.**
- **Precedent for unknown-type injection**: `test_scan_integrity.py` /
  `test_projection.py` hand-build a `widget.exploded` body + `hash_event(body)`.
  We reuse this pattern but route it **through `append_event`** (below) so
  sequencing stays honest/gapless rather than raw-appending to a month file.

---

## Design decisions (pinned)

### 1. Location, and the in-process / subprocess split

- **Location: `cli/tests/test_equivalence_gate.py`.** The projection under test is
  CLI-side (`cli/msgctl`), so the rule ("`server/` vs `cli/` follows the code under
  test") puts it in `cli/tests`. The M1 server-projection extension will add a
  sibling in `server/tests`; the M2 Dexie extension lands in `web/`. Documented in
  the module docstring.
- **Property loop runs IN-PROCESS** — `resolve_or_create_stream` + `append_event`
  (send), `open_db`/`project`/`dump_messages` (project), `rebuild_projection`
  (rebuild), `verify.verify_workspace` (verify) as **library calls**. Rationale:
  under hypothesis (dozens of examples × up to ~30 fsync'd appends each) subprocess
  spawn per send would blow the CI time budget. In-process keeps the whole property
  loop to ~30–60s.
- **Plus ONE subprocess end-to-end smoke slice** — a plain (non-`@given`) test that
  drives the **real CLI** via `conftest.run_cli`: `init → send×N (2 streams, incl.
  one unicode text) → project → rebuild → verify`, asserting each exits 0. It reads
  the resulting `projections.sqlite3` dump in-process before and after `rebuild` and
  asserts equality — this is the honest proof that argparse → `cmd_project` →
  `cmd_rebuild` → `cmd_verify` wiring is intact end to end. Kept small (~6 sends) so
  it costs ~1s.

### 2. Hypothesis strategies (pin exactly)

A `@st.composite` `_send_plan(draw)` producing a `SendPlan`:

- `n_streams = draw(st.integers(min_value=1, max_value=4))`; stream names
  `[f"s{i}" for i in range(n_streams)]` (auto-created on first send).
- `actions = draw(st.lists(_action(streams), min_size=0, max_size=30))` — a flat,
  **randomly-interleaved** action list across streams (interleaving falls out of the
  flat list; per-stream sequencing is handled by `append_event`). `min_size=0`
  covers the empty-workspace edge (dump == "", still must be equivalent + verify 0).

Each `_action` is one of:

- **`message.created`** (~90%, via weighted `st.sampled_from`/`st.integers`):
  - `stream`: `st.sampled_from(streams)`
  - `text`: `st.text(st.characters(codec="utf-8"), min_size=0, max_size=200)`.
    **`codec="utf-8"` is load-bearing**: it excludes lone surrogates (U+D800–DFFF),
    which `append`'s `.encode("utf-8")` and JCS both reject upstream — we generate
    only text msgctl can actually store. Full unicode (incl. emoji, CJK, combining
    marks) is otherwise in range, exercising `ensure_ascii=False`. Empty text is
    valid (`MessageCreatedV1` has no min length).
  - `format`: `st.sampled_from(["markdown", "plain"])`
  - `thread_root_id`: `st.none() | st.builds(ids.new_message_id)` (format-only
    validated; existence not checked at M0)
  - `mentions`: `st.lists(st.builds(ids.new_user_id), max_size=3)`
  - `file_ids`: `st.lists(st.builds(ids.new_file_id), max_size=3)`
    (mentions/file_ids exercise projection+verify robustness; they are **not** in
    `_DUMP_COLUMNS`, so they don't affect the dump — `thread_root_id` **is**.)
- **`unknown.type`** (~10%): injected via `append_event` with a hand-built `Body`
  (`type="widget.exploded"`, `type_version=7`, opaque `payload={"blast_radius": …}`),
  `event_hash=hash_event(body.model_dump(mode="json"))`, `workspace_id=ws.workspace_id`,
  author from `ws.local_author`. Goes through the real sequencer so it consumes a
  gapless `server_sequence`; the projection must **skip** it (D9) and verify must
  treat it as a note, not a finding.

Two in-process send helpers wrap `append_event` (both set `server` from the callback
args exactly like `cmd_send`): `_send_created(ws, stream, text, **opts)` and
`_send_unknown(ws, stream)`.

### 3. The equivalence assertion (the property body)

Per example, in a **fresh per-example workspace dir** (see Risk R1):

1. `init_workspace(root)`; `ws = Workspace.open(root)`.
2. **Build incrementally**: iterate `actions`; after **each** append call
   `project(ws, conn)` on a single persistent `open_db` connection (genuine
   incremental stepping of the per-stream cursors, not one batch at the end).
3. `dump_incremental = dump_messages(conn)`; **close conn** (before rebuild swaps
   the live DB file).
4. **Rebuild**: `rebuild_projection(ws)` (real ENG-59 path — temp DB + atomic swap);
   reopen `open_db(live)`; `dump_rebuilt = dump_messages(conn2)`.
5. **Assert `dump_incremental == dump_rebuilt`** — rebuild ≡ incremental, the gate.
6. **Idempotence**: `result = project(ws, conn2)` once more → assert
   `result.applied == 0` **and** `dump_messages(conn2) == dump_rebuilt`; close conn2.
7. **Verify**: `assert verify.verify_workspace(root).exit_code == 0` (independent of
   the projection DB — verify walks logs only and ignores `projections.sqlite3` by
   path).

Note on determinism of content: `dump_messages` includes `client_created_at` /
`server_received_at` (wall-clock at append time), so the dump **text** differs run to
run — but within one example both incremental and rebuild read the **same log**, so
they carry identical timestamps and the equality holds. The gate asserts *equivalence
within a run*, never reproducibility of dump bytes across runs.

### 4. Mutation test (acceptance: "a deliberately introduced projection bug makes it fail")

Cannot commit a real bug. Instead a **separate, non-property test** that proves the
comparison has **teeth**, by monkeypatching the projection handler for the **rebuild
pass only** so exactly one row diverges:

1. Build a small deterministic workspace (a few `message.created` across 2 streams),
   incrementally → `dump_incremental`.
2. **Positive control**: a clean `rebuild_projection` → `dump_clean`; assert
   `dump_clean == dump_incremental` (proves the equality fires on a correct build).
3. `monkeypatch.setitem(projection._HANDLERS, ("message.created", 1), corrupt)` where
   `corrupt` calls the real `_apply_message_created` then mutates **one** row (e.g.
   `UPDATE messages SET text = text || 'X' WHERE server_sequence = 1 …`). Run
   `rebuild_projection(ws)` again → `dump_corrupt`.
4. **Assert `dump_corrupt != dump_incremental`** — i.e. a single-cell projection
   corruption *would* make the gate's `==` assertion raise. Patching the **rebuild
   side only** (not globally) is essential: a global patch would corrupt both sides
   identically and they'd still match, demonstrating nothing.

This is documented as the standing proof that the gate detects divergence; it lives in
the same file so the gate and its teeth-check are never separated.
(`test_projection.py::test_crash_mid_apply_converges` already establishes the
`monkeypatch.setitem(_HANDLERS, …)` pattern — reuse it.)

### 5. Permanence marking

- **Module docstring** opens with a bold banner:
  `PERMANENT GATE — never delete. rebuild ≡ incremental is a permanent projection
  invariant (TDD §5 M0 exit criterion, §12 invariant 6). Extend at M1 (server
  projections, server/tests) and M2 (client Dexie, web/); never remove.`
- **CI-visible name**: a dedicated CI step named
  `Equivalence gate (rebuild ≡ incremental)` (see §6) gives the check a stable,
  greppable identity for branch-protection "required checks".

### 6. CI wiring

The file is already collected by the existing `Pytest` step (`cli/tests` ∈
`testpaths`), so "runs in CI" is satisfied for free. We **additionally** add one
named step in the `checks` job of `.github/workflows/ci.yml`, placed **after Mypy and
before the full `Pytest` step**, so a gate failure surfaces first and is instantly
localized:

```yaml
      - name: Equivalence gate (rebuild ≡ incremental)
        run: uv run pytest cli/tests/test_equivalence_gate.py
```

Lean "yes" on the extra step: one line, negligible extra time (the gate file is fast),
huge failure-visibility win — a red build names the invariant directly instead of
burying it in the full-suite output. The full `Pytest` step re-runs it harmlessly.

**Required-check follow-up (ops, out of code):** mark
`lint · type · test` (the job) — or, if per-step required checks are wanted, the job —
required in GitHub branch protection for `main`. The named step gives it a stable
identity. Flag to the repo admin; not a file change.

### 7. Determinism profile

Register two hypothesis profiles at module import and load by environment:

```python
settings.register_profile("ci",  max_examples=60,  deadline=None,
                          derandomize=True, database=None)
settings.register_profile("dev", max_examples=100, deadline=None)
settings.load_profile("ci" if os.environ.get("CI") == "true" else "dev")
```

- **CI**: `derandomize=True` → deterministic example selection from a fixed internal
  seed (reproducible, satisfies "deterministic under fixed seed"); `database=None` →
  hermetic (no replay of a locally-found failure DB); `deadline=None` → no per-example
  deadline flakes from fsync/SQLite IO variance; `max_examples=60` → ~30–60s budget.
  (GitHub Actions sets `CI=true` automatically.)
- **Local (`dev`)**: random, `max_examples=100`, so developers keep finding new cases.
- Documented in the module docstring.

---

## File list

| File | Action | Agent |
|------|--------|-------|
| `cli/tests/test_equivalence_gate.py` | **create** — property gate + subprocess smoke + mutation/teeth test + profiles + permanence docstring | python-engineer |
| `.github/workflows/ci.yml` | **modify** — add one named `Equivalence gate` step in the `checks` job | devops-engineer |

No production-code, dependency, or `pyproject.toml` changes.

## Step-by-step (python-engineer, the test file)

1. Module docstring with the **PERMANENT GATE** banner (§5) and the in-process /
   subprocess + determinism rationale.
2. Imports; register + load the two hypothesis profiles (§7).
3. Helpers: `_fresh_ws(base) -> Path` (per-example `tempfile.mkdtemp` under a base +
   `init_workspace`), `_send_created(ws, stream, **opts)`, `_send_unknown(ws, stream)`
   (both wrap `append_event`, setting `server` from the callback args like `cmd_send`).
4. `@st.composite _send_plan` per §2.
5. `test_rebuild_equals_incremental_property` — `@given(_send_plan())`, body per §3,
   with a `try/finally` `shutil.rmtree` of the per-example dir (**R1**).
6. `test_gate_detects_single_row_divergence` — the mutation/teeth test per §4.
7. `test_cli_end_to_end_smoke` — the subprocess slice per §1 (real `run_cli`
   init/send/project/rebuild/verify all exit 0 + in-process dump equality across
   rebuild).

## Test plan

The file **is** the test. Coverage matrix:

- rebuild ≡ incremental over randomized multi-stream interleavings incl. unicode,
  optional fields, and injected unknown types (property).
- verify exit 0 on every generated workspace (property).
- idempotent re-project: applied 0, dump unchanged (property).
- empty-workspace edge (`min_size=0` → dump == "", equivalence + verify still hold).
- gate has teeth: one-row divergence is detected (mutation test).
- real CLI wiring init→send→project→rebuild→verify (subprocess smoke).

Local run: `uv run pytest cli/tests/test_equivalence_gate.py` (dev profile, random).
CI run: same file as a named step (ci profile, derandomized) + again in full `Pytest`.

## Risks / open questions

- **R1 (correctness gotcha, must-fix): hypothesis + the `tmp_path` fixture.** A
  `@given` test runs its body **many times with the same fixture values** — the pytest
  `tmp_path` fixture yields **one** dir reused across all examples, cross-contaminating
  workspaces. The gate **must** mint a fresh dir per example itself
  (`tempfile.mkdtemp`) and `rmtree` it in `finally`. Called out explicitly so the
  implementer doesn't reach for `tmp_path` inside `@given`.
- **R2 (CI time budget / flakiness).** Each example does up to ~30 fsync'd appends +
  a `project` per append + a full `rebuild` + a `verify`. `deadline=None` removes
  per-example deadline flakes; `max_examples=60`, `max_size=30`, `n_streams≤4` keep it
  to ~30–60s. If CI proves slower than budget, first lever is `max_examples`, second is
  projecting every *k* actions instead of every action (still incremental). Do **not**
  drop `deadline=None` — fsync IO variance would flake it.
- **R3 (append fsync cost).** `append_event` fsyncs every write (correct for the log);
  under hypothesis this is the dominant cost. Acceptable on ubuntu CI; the knobs in R2
  bound it. Not worth bypassing the real append path — honest sequencing is the point.
- **R4 (rebuild swaps the live DB).** `rebuild_projection` overwrites
  `projections.sqlite3`. The gate captures `dump_incremental` and **closes the conn**
  *before* calling rebuild, then reopens — pinned in §3 steps 3–4. Getting this order
  wrong compares a dump against a half-swapped file.
- **R5 (branch-protection required check is an ops action, not a file).** The named CI
  step provides the identity; an admin must mark it required for `main`. Flagged for
  the repo owner.

## Summary for the implementers

- **Split**: hypothesis property loop **in-process** (`append_event` / `project` /
  `dump_messages` / `rebuild_projection` / `verify_workspace` as library calls) for
  speed; **one** subprocess `run_cli` smoke (init→send→project→rebuild→verify) for
  honest end-to-end wiring.
- **Strategy**: 1–4 streams, 0–30 randomly-interleaved actions; ~90%
  `message.created` (`st.text(st.characters(codec="utf-8"))` unicode-safe, empty ok;
  format; optional `thread_root_id`/`mentions`/`file_ids`), ~10% injected
  `widget.exploded` unknown-type via `append_event` (real hash, real sequence, D9
  skip).
- **Assertion**: `dump_messages(incremental) == dump_messages(rebuilt)` byte-equal,
  `verify_workspace(root).exit_code == 0`, and re-`project` → applied 0 & dump
  unchanged.
- **Mutation test**: monkeypatch `_HANDLERS[("message.created",1)]` for the **rebuild
  pass only** to corrupt one row post-insert; assert the two dumps now differ (proves
  the `==` has teeth) plus a clean positive control that they match.
- **CI**: file auto-runs in the existing `Pytest` step (`cli/tests` ∈ `testpaths`);
  add one named `Equivalence gate (rebuild ≡ incremental)` step after Mypy for
  instant failure isolation. No dep/config change (`hypothesis>=6.100` already
  present).
- **Determinism**: `settings.register_profile("ci", derandomize=True, deadline=None,
  database=None, max_examples=60)` loaded when `CI=true`; local `dev` profile stays
  random. Permanence via module docstring banner + the named CI step.
- **Agents**: python-engineer (test file), devops-engineer (one CI step line).
