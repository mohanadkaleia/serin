# ENG-54 — M0: Event envelope + payload schemas in core/ (Pydantic v2)

**Milestone:** M0 — Protocol spike
**Priority:** High · **Status:** In Progress · **Assignee area:** all `python-engineer`
**Branch:** `mohanad/eng-54-m0-event-envelope-payload-schemas-in-core-pydantic-v2`
**TDD refs:** §2.1 (envelope), §2.2 (event types — `message.created` only in this ticket), §2.3 (schema-evolution contract), §4.1 (stack — Pydantic v2 locked), §1.1 (repo layout, `core/` is shared server+CLI). Locked decisions: **D1** (body/server split, JCS-hashed body, `signature` reserved null), **D9** (per-type versions, additive-only, unknown types preserved-not-crashed), **D14** (`client_created_at` untrusted).

> **This schema gets LOCKED at M0 exit.** Every field name, default, and serialization shape decided here is a contract the web client (M2, TypeScript), exports (M4), and plugins (M5) inherit. Bias every ambiguous call toward *lossless, verbatim, forward-compatible* over *convenient*.

---

## Goal (restated)

Produce the Pydantic v2 models and helpers in `server/msgd/core/` that define the wire/storage shape of an event exactly as TDD §2.1 draws it: a hashed client `body`, an unhashed `server` metadata block, top-level `event_hash` + reserved-null `signature`. Ship the `message.created` v1 payload model, a client-mintable typed-ULID id module, a 64 KB size check, and the §2.3 schema-evolution plumbing (unknown fields and unknown *types* round-trip without loss or crash).

This ticket is **independent of ENG-55 (JCS) and ENG-56 (hashing)** by construction: `event_hash` is an opaque string here, computed and verified later. That independence is deliberate so the three M0 core tickets can land in parallel.

Areas touched: `server/msgd/core/` (new modules), `server/pyproject.toml` (deps), root `pyproject.toml` (dev group), `server/tests/` (new tests + fixture), plus the tiny `py.typed` packaging carryover from PR #1 review.

---

## Module-layout decision

Inside `server/msgd/core/`:

```text
server/msgd/
  py.typed              # NEW (PR #1 carryover): PEP 561 marker so msgd ships typed
  core/
    __init__.py         # UNCHANGED — ENG-54 makes ZERO edits here (see collision note)
    ids.py              # NEW — typed-ULID minting + parse/validate API
    envelope.py         # NEW — Body / ServerMetadata / Envelope models, size cap, constants
    payloads/           # NEW package
      __init__.py       # NEW — payload registry: (type, version) -> model; get_payload_model()
      message.py        # NEW — MessageCreatedV1 payload model
```

**Why a `payloads/` package rather than a flat `schemas.py`:** §2.2 lists ~20 payload types arriving across M1–M5 (messages, reactions, edits, membership, files, bots). A package gives each type family its own module (`message.py`, later `reaction.py`, `membership.py`, `file.py`) and one registry seam, instead of one file that every future ticket edits and conflicts on. It also sidesteps the `core/__init__.py` docstring's placeholder name `schemas` — we deliberately do not create a `schemas.py`.

**Collision avoidance with the parallel tickets (hard constraints):**

- **ENG-55 owns `core/jcs.py` and its tests only.** ENG-54 creates neither `jcs.py` nor any test file ENG-55 would create. No ENG-54 module imports `jcs` (size check uses stdlib `json`; hashing is out of scope).
- **ENG-56 owns `core/hashing.py`, `core/testdata/`, and `core/testdata/vectors.json`.** ENG-54 creates **no** `core/testdata/` directory and **no** `hashing.py`. ENG-54's own test fixture (the §2.1 example) lives under `server/tests/` (see test plan), never under `core/testdata/`, so the two never collide.
- **`core/__init__.py` is touched by both ENG-54 and ENG-55.** To keep the merge trivial, **ENG-54 does not edit `__init__.py` at all** — no re-exports. Consumers import submodules directly (`from msgd.core.envelope import Envelope`, `from msgd.core import ids`). This leaves ENG-55 free to add its single line (or not) with zero conflict. If a curated public surface is ever wanted, it is a later, separate decision.
- **Shared edited files** (`server/pyproject.toml` dependency list, root `pyproject.toml` dev group, `uv.lock`) are edited additively by both ENG-54 and ENG-55 — see Risks for the merge/lock coordination.

---

## Key modeling decisions (these are the locked calls — read before coding)

1. **`extra="allow"` everywhere on the envelope path (the crux of §2.3).** `Body`, `ServerMetadata`, and `Envelope` set `model_config = ConfigDict(extra="allow")`. This is what makes *unknown fields survive a round trip* (they are retained as model attributes and re-emitted by `model_dump`) while readers still "ignore" them semantically (nothing reads them). Pydantic's default `extra="ignore"` would silently drop them and fail the acceptance criterion — so this config is load-bearing, not stylistic.

2. **`payload` is stored as `dict[str, Any]` on `Body`, never a typed union.** A discriminated/typed union would raise on an unknown `type` — violating D9's "never crash". Keeping `payload` an opaque dict guarantees **unknown event types round-trip losslessly**. Known payloads are validated *on demand* via the registry (`get_payload_model(type, type_version)` → validate `body.payload`), not by coercing them into the envelope. `MessageCreatedV1` is used to **build** and to **validate**, but the wire model keeps `payload` as a dict.

3. **All timestamps are stored as validated `str`, not `datetime`.** `client_created_at` and `server.server_received_at` are kept as strings (validated to RFC 3339 / ISO-8601 with a `Z` or offset). Rationale: the acceptance criterion is *byte-lossless* re-serialization of the §2.1 example, and Pydantic's `datetime` round-trip mutates the text (`.123Z` → `.123000Z`, `Z` vs `+00:00`). More importantly, ENG-56 hashes JCS(body); the body must serialize back to exactly what the client sent, so timestamp text must be preserved verbatim. D14 makes `client_created_at` untrusted metadata anyway — there is no reason to parse it into a `datetime` here. This is a deliberate lock; document it in `envelope.py`.

4. **`event_hash: str` required and opaque; `signature: str | None = None`; `server: ServerMetadata | None = None`.**
   - `event_hash` is a plain string (`"sha256:..."`) here — ENG-54 does not compute or verify it (ENG-56 does). Tests assemble full envelopes with a placeholder/fixture hash.
   - `signature` is reserved: type `str | None`, **default `None`**, always null in MVP (D1). Present-and-defaulted is an acceptance criterion.
   - `server` is **optional** because the two wire forms differ: the client upload body (`POST /v1/events/batch`, §3.2) sends `{ "body": {...}, "event_hash": "..." }` with **no** `server` block; the stored/served form (§2.1) has it. `server: ServerMetadata | None = None` models both. `ServerMetadata` holds `server_sequence: int`, `server_received_at: str`, `payload_redacted: bool = False` (reserved, ships now — default present is an acceptance criterion).

5. **`event_id` is a bare ULID (no prefix); all entity ids are prefix+ULID.** Per the §2.1 example, `event_id` is `"01JZ7N6A4M6Y8W5K2H7DGKX4PA"` — no prefix. Entity ids carry type prefixes: `w_ u_ s_ m_ f_ d_`. `ids.py` reflects this distinction: `new_event_id()` returns a bare ULID; `new_workspace_id()` etc. return prefixed ids.

6. **Monotonic-safe ULID minting is implemented in `ids.py`, library-agnostic.** We use the ULID library (`python-ulid`) only for encode/decode/validity, and wrap minting in a small monotonic factory (module-level, lock-guarded): track the last (timestamp_ms, randomness); within the same millisecond, increment the randomness so ids are *strictly increasing lexicographically*, never colliding or going backwards. This is required because `server_sequence` does not exist client-side and downstream tickets rely on ULID sort order; do not rely on the library's default random-per-call behavior for monotonicity.

7. **Size cap = 64 KB over the serialized event.** `MAX_EVENT_SIZE_BYTES = 64 * 1024` and `check_event_size(envelope) -> None` (raises `EventTooLargeError`) / `serialized_size_bytes(envelope) -> int`. Measure `len(json.dumps(envelope.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))`. Measured over the full envelope as constructed (compact, no whitespace). The authoritative *enforcement point* (upload) arrives in M1; the constant + helper live in core now so the CLI (M0) and server (M1) share one definition.

---

## Implementation plan (all steps: **python-engineer**)

### Step 1 — Dependencies

- **`server/pyproject.toml`** → `[project].dependencies`: add
  - `pydantic>=2.7` (Pydantic v2 is locked, §4.1)
  - `python-ulid>=2.2`  — **PyPI name `python-ulid`, import name `ulid`** (`from ulid import ULID`). Do **not** install `ulid-py`, which also imports as `ulid` and would clash; pin the PyPI name explicitly.
  - Nothing else — the constraint is pydantic v2 + one ULID lib only.
- **Root `pyproject.toml`** → `[dependency-groups].dev`: add `hypothesis>=6.100`. (ENG-55 also wants hypothesis; additive — whoever merges second dedupes. Harmless if listed twice pre-merge.)
- Run `uv sync` to install and regenerate `uv.lock`; commit the lock. Confirm `python -c "import pydantic, ulid"` resolves in the workspace venv.

### Step 2 — `server/msgd/core/ids.py`

- Prefix constants + a frozenset of valid entity prefixes: `w_ u_ s_ m_ f_ d_`. Model the kinds (an `IdKind` `StrEnum` mapping kind→prefix is fine, or plain constants).
- Monotonic factory: `new_ulid() -> str` returns a 26-char Crockford-base32 ULID, strictly increasing across calls (lock-guarded, per decision 6).
- `new_event_id() -> str` → bare ULID.
- `new_typed_id(prefix: str) -> str` → `prefix + new_ulid()`; plus thin convenience wrappers `new_workspace_id / new_user_id / new_stream_id / new_message_id / new_file_id / new_device_id`.
- Parse/validate API (needed by later reactions/edits tickets to validate referenced ids — this is an explicit constraint):
  - `is_valid_ulid(value: str) -> bool` — 26 chars, valid Crockford base32, decodes.
  - `is_valid_typed_id(value: str, prefix: str) -> bool` — prefix matches **and** remainder is a valid ULID.
  - `parse_typed_id(value: str, *, expected_prefix: str | None = None) -> ParsedId` — returns `(prefix, ulid)`, raises `ValueError` on bad prefix or bad ULID. (`ParsedId` = small frozen dataclass or `NamedTuple`.)
- Fully typed (mypy strict), no server-only imports (keep `core` CLI-cheap per §1.1 / the `__init__.py` docstring constraint).

### Step 3 — `server/msgd/core/payloads/message.py`

- `MessageCreatedV1(BaseModel)` with `model_config = ConfigDict(extra="allow")` (additive-only forward compat, §2.3.2):
  - `message_id: str` — validated as an `m_` id (`is_valid_typed_id(..., "m_")`).
  - `text: str`
  - `format: str` — see open question Q1 (recommend `Literal["markdown", "plain"]` with default `"markdown"`; falls back to plain `str` if we want zero constraint at lock time).
  - `thread_root_id: str | None = None` — when set, validated as `m_` id (D7: first reply *is* the thread; same stream).
  - `file_ids: list[str] = []` — each validated as `f_` id.
  - `mentions: list[str] = []` — each validated as `u_` id.
- Optional-field defaults are the locked shape; `?` fields in §2.2 map to `None` / `[]` defaults so absent-in-JSON round-trips cleanly.
- Id-prefix validation here exercises the `ids` validate API and catches malformed references early; note (Q2) that *referential existence* checks (does the message exist?) are server-side (§3.2) and out of scope — this is format-only validation.

### Step 4 — `server/msgd/core/payloads/__init__.py`

- Registry: `PAYLOAD_MODELS: dict[tuple[str, int], type[BaseModel]] = {("message.created", 1): MessageCreatedV1}`.
- `get_payload_model(type: str, type_version: int) -> type[BaseModel] | None` — returns `None` for unknown (type, version): the caller then treats the payload as opaque (D9 "skip in projection, never crash").
- `build_message_created_body(...) -> Body` convenience for client minting (mints `event_id` + `message_id`, builds and validates a `MessageCreatedV1`, dumps it to `payload` dict, assembles a `Body`). Envelope finalization (attaching `event_hash` + `server`) is left to ENG-56/M1; tests here wrap the body with a placeholder hash.

### Step 5 — `server/msgd/core/envelope.py`

- `ServerMetadata(BaseModel, extra="allow")`: `server_sequence: int`, `server_received_at: str`, `payload_redacted: bool = False`.
- `Body(BaseModel, extra="allow")`: `event_id: str`, `workspace_id: str`, `stream_id: str`, `type: str`, `type_version: int`, `author_user_id: str`, `author_device_id: str`, `client_created_at: str`, `payload: dict[str, Any]`. (Prefix validation on `workspace_id`/`stream_id`/author ids optional — recommend validating them via `ids` since they are always our own ids; `event_id` validated as bare ULID.)
- `Envelope(BaseModel, extra="allow")`: `body: Body`, `event_hash: str`, `signature: str | None = None`, `server: ServerMetadata | None = None`.
- Constants + helpers: `MAX_EVENT_SIZE_BYTES = 64 * 1024`; `serialized_size_bytes(envelope) -> int`; `EventTooLargeError`; `check_event_size(envelope) -> None`.
- Timestamp validation helper (str, RFC 3339-ish) shared by `Body.client_created_at` and `ServerMetadata.server_received_at`.
- Module docstring records the three locked calls (extra=allow, opaque dict payload, str timestamps) so a future reader understands why they cannot be "cleaned up".

### Step 6 — `py.typed` packaging carryover (PR #1 review, tiny)

- Create empty `server/msgd/py.typed` (PEP 561 marker) so downstream typecheckers see `msgd` as typed.
- Ensure the build ships it. Hatchling includes files under the `packages = ["msgd"]` tree by default, but the review asked for it explicitly: after adding, run `uv build` (or `python -m hatchling build`) on `server/` and confirm `py.typed` is in the wheel (`unzip -l dist/*.whl | grep py.typed`). If absent, add to `server/pyproject.toml`:
  `[tool.hatch.build.targets.wheel] artifacts = ["msgd/py.typed"]` (or `force-include`). Record the actual outcome in the PR.

### Step 7 — Local gates

Run and pass before opening the PR: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`. Report actual output.

---

## Test plan (pytest + hypothesis) — files under `server/tests/`

All ENG-54 test files are namespaced to avoid any overlap with ENG-55/56 test files. The **§2.1 example fixture lives at `server/tests/data/eng54_envelope_example.json`** (NOT `core/testdata/` — that path belongs to ENG-56).

**`server/tests/data/eng54_envelope_example.json`** — the exact §2.1 example JSON, verbatim (the locked reference).

**`server/tests/test_envelope.py`**
- `test_2_1_example_round_trips_losslessly` — load the fixture, `Envelope.model_validate(...)`, `model_dump(mode="json")`, assert **deep-equal to the original parsed JSON** (the headline acceptance criterion).
- `test_unknown_body_field_survives` / `test_unknown_payload_field_survives` / `test_unknown_server_field_survives` — inject an extra key at each level, assert it is present after parse→dump (proves `extra="allow"`).
- `test_unknown_event_type_round_trips` — `type="widget.exploded"`, `type_version=7`, arbitrary `payload`; parse→dump lossless, no exception (D9).
- `test_signature_defaults_null` and `test_payload_redacted_defaults_false` — omit them from input; assert `signature is None` and `server.payload_redacted is False` (acceptance criterion: present + defaulted).
- `test_client_upload_form_has_no_server` — `{body, event_hash}` only parses with `server is None`.
- `test_round_trip_per_registered_type_version` — parameterized over every `(type, version)` in `PAYLOAD_MODELS` (currently just `message.created` v1): build a valid event, round-trip it losslessly, and validate the payload with `get_payload_model`.
- `test_size_cap_rejects_oversized` — a `message.created` with `text` pushing serialized size past 64 KB → `check_event_size` raises `EventTooLargeError`; a normal event passes. Also assert an event exactly at the boundary behaves as specified.
- Hypothesis: `test_arbitrary_body_round_trips` — strategy generating bodies with random unknown fields / arbitrary JSON payloads; assert `model_dump(model_validate(x)) == x` (property form of losslessness, covers unknown-type preservation broadly).

**`server/tests/test_payloads.py`**
- `MessageCreatedV1` required-field enforcement; optional defaults (`thread_root_id=None`, `file_ids=[]`, `mentions=[]`).
- Id-format validation: bad `message_id` prefix, non-`u_` mention, non-`f_` file id → `ValidationError`.
- `format` behavior per the Q1 decision.
- `get_payload_model` returns the model for known (type, version) and `None` for unknown.

**`server/tests/test_ids.py`**
- Each `new_*_id()` produces the right prefix + a valid ULID; `new_event_id()` is a bare ULID (no prefix).
- Monotonicity: mint N ids in a tight loop (same millisecond) → assert strictly increasing lexicographically (`ids == sorted(ids)` and no duplicates). Optionally freeze/patch time to force same-ms collisions.
- Parse/validate: valid id parses; wrong prefix, wrong length, non-Crockford chars → `is_valid_* == False` / `parse_typed_id` raises.
- Hypothesis: `parse_typed_id(new_typed_id(p)).prefix == p` round-trip across all prefixes.

---

## Risks / open questions

- **Q1 — `format` domain (LOCK-sensitive).** §2.1 shows only `"markdown"`. Recommend `Literal["markdown", "plain"]`, default `"markdown"` — enough to be useful, additive-friendly (new values are a `type_version` bump per §2.3). If we prefer zero constraint at lock time, use `format: str`. Decision needed before merge since the schema locks at M0 exit. **Recommendation: `Literal["markdown", "plain"]`.**
- **Q2 — Id-prefix validation in payloads.** Including `m_`/`u_`/`f_` prefix checks on `MessageCreatedV1` couples payloads to `ids` and could reject a technically-valid-but-unexpected id. It is cheap, catches client bugs early, and exercises the `ids` API the later tickets need. Referential *existence* checks stay server-side (§3.2). **Recommendation: keep format-only prefix validation.**
- **Size-cap measurement point.** Core defines the constant + a compact-JSON measurement; the binding *reject* happens at upload in M1 (§3.2/§4.3). Ensure the M1 API reuses `check_event_size` rather than re-deriving 64 KB, so there is one definition. Noted for the M1 ticket, not blocking here.
- **`datetime`-as-string is a deliberate lock.** If a later consumer wants a parsed timestamp, it parses the string itself (D14 says nothing trusts `client_created_at` for ordering anyway). Do not "upgrade" these to `datetime` — it breaks byte-lossless round-trip and JCS reproducibility. Called out in `envelope.py` docstring.
- **Merge coordination with ENG-55 (parallel).** Shared additive edits: `server/pyproject.toml` deps (ENG-55 adds `rfc8785` or vendors JCS), root `pyproject.toml` dev group (both add `hypothesis`), and `uv.lock`. Plan: land ENG-54 first if possible; whoever merges second re-runs `uv sync` to regenerate `uv.lock` (do not hand-merge the lock) and dedupes `hypothesis`. `core/__init__.py`: ENG-54 makes **zero** edits, so no conflict there by design. `core/testdata/` and `jcs.py`/`hashing.py` are untouched by ENG-54, so no file-level collision with ENG-55/56.
- **ULID library API surface.** Confirm `python-ulid`'s `ULID` exposes construction from timestamp+randomness and a validity/parse path; the monotonic factory is implemented in `ids.py` regardless, using the library only for encode/decode. If `python-ulid`'s API is awkward for the monotonic increment, the fallback is to keep the 48-bit time + 80-bit randomness handling in `ids.py` directly (still no new dependency).
- **Envelope assembly vs. hashing boundary.** ENG-54 stops at `Body` builders + an `Envelope` model with an opaque `event_hash`. Full seal (compute hash, attach server metadata) is ENG-56/M1. Tests use placeholder hashes. This keeps ENG-54 mergeable without ENG-55/56 present.

---

## Review Round 1 — Triage & Fix Plan

Reviewer verdict: REQUEST_CHANGES (comment form, own-PR). Five findings on PR #3. One substantive (size-cap form), four nits. This is a **locked-schema correctness round, not a refactor** — scope is deliberately minimal. Implementer: `python-engineer`. Decisions below are final for the M0 lock.

**Summary of dispositions**

| # | Finding | Severity | Decision |
|---|---|---|---|
| 1 | Size cap measured over full `Envelope`, not §3.2 `{body, event_hash}` wire form | substantive | **ADDRESS** |
| 2 | `format` has default `"markdown"` but §2.2 lists it required | nit | **DECIDE → keep default** (record rationale) |
| 3 | RFC 3339 regex accepts out-of-range instants | nit | **DECIDE → keep shape-only** + add scope comment |
| 4 | `test_unknown_payload_field_survives` doesn't exercise `extra="allow"` | nit | **ADDRESS** (comment + real payload-config guard) |
| 5 | Headline round-trip is structural, not byte-verbatim | nit/info | **PUSH BACK** (document rationale; add clarifying comment) |

---

### Finding 1 — Size cap must measure the §3.2 upload wire form — ADDRESS

**Why it's right.** §2.1 defines the cap as "hard reject **at upload**" and §3.2 fixes the upload wire form as exactly `{ "body": {...}, "event_hash": "sha256:..." }` — no `signature`, no `server`. Measuring the full `Envelope` makes the size of an identical client `body` depend on whether server metadata has been attached (not form-stable), and over-counts even at the upload point by the two null keys (`,"signature":null,"server":null`). Since this definition is inherited downstream (TS client M2, exports M4), it must be pinned to the uploaded bytes **before the schema locks**. This also corrects the plan's own Step-5 "compact-JSON of the envelope" wording, which was imprecise.

**Fix — `server/msgd/core/envelope.py`.** Change `serialized_size_bytes` to measure **only** the wire form, independent of `signature`/`server`:

```python
def serialized_size_bytes(envelope: Envelope) -> int:
    """UTF-8 byte length of the §3.2 upload wire form ``{body, event_hash}``.

    The cap is defined over the bytes the client uploads (§2.1 "hard reject at
    upload", §3.2 wire form), NOT the full stored envelope — ``signature`` and
    ``server`` are excluded so the measured size is stable for a given ``body``
    regardless of whether server metadata has been attached.
    """
    wire = {"body": envelope.body.model_dump(mode="json"), "event_hash": envelope.event_hash}
    compact = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)
    return len(compact.encode("utf-8"))
```

`check_event_size` is unchanged (it delegates to `serialized_size_bytes`). Update the docstring reference in the module header if it implies whole-envelope measurement.

**Tests — `server/tests/test_envelope.py`.**
- `test_size_cap_boundary` and `test_size_cap_accepts_normal_event` currently build from `_valid_envelope_dict()` (which carries a `server` block). They still pass under the wire-form measurement because padding is computed via the same function — but the intent is now different, so update the comments to say the boundary is over `{body, event_hash}`.
- **Add `test_size_cap_is_form_stable`** — the key new guarantee: build one `body`+`event_hash`, measure it once with `server=None` and once with a `ServerMetadata` attached, assert `serialized_size_bytes` is **identical** across both. This is the regression that locks the fix.

Not blocking on M1: the binding reject still lives at upload (§3.2/§4.3) and must reuse `check_event_size` — already noted in Risks.

### Finding 2 — `format` default `"markdown"` vs. §2.2 "required" — DECIDE: keep the default

**Ruling: keep `format: Literal["markdown", "plain"] = "markdown"`.** This confirms plan Q1 as the M0 lock. Rationale, recorded now because field-requiredness locks here:

- `MessageCreatedV1` is a **validation-only** view (§3.2 "schema (type + version)" check). It never round-trips back into the stored `body` — the body's `payload` is the opaque dict preserved verbatim (locked call 2). So defaulting does **not** mutate stored bytes and cannot cause a hash mismatch: a client that omits `format` stores a payload with no `format` key, and the hash is over exactly those bytes.
- "Required" in the §2.2 table is the **payload contract** (`format` is always meaningful for a message), not a wire-level "reject if absent." Defaulting satisfies the contract: every validated `MessageCreatedV1` has a well-defined `format`, and markdown is the safe, useful default.
- Additive-friendly per §2.3/D9: new format values arrive via a `type_version` bump, so the `Literal` is not an evolution hazard.

No code change. `python-engineer`: keep the existing `format` field and its test; ensure a `test_payloads.py` case asserts that omitting `format` yields `"markdown"` (documents the locked behavior).

### Finding 3 — RFC 3339 regex is shape-only — DECIDE: keep shape-only, add a scope comment

**Ruling: do not tighten the regex; document the scope.** Range validation (rejecting month 13, hour 99) has **no correctness value here** and is not worth the fiddly regex/parse:

- `client_created_at` is untrusted metadata (D14) — nothing orders or displays on it as authority; server sequence/time are authoritative.
- `server_received_at` is server-minted, so it is never out-of-range in practice.
- The lock we care about is **verbatim preservation** (str, not `datetime`), which shape-only validation already guarantees. A stricter validator would add surface area with zero downstream benefit.

**Fix — `server/msgd/core/envelope.py`.** One-line scope comment at `_RFC3339_RE` / `_validate_rfc3339` making the deliberate choice explicit, e.g.: `# Shape-only: we validate structure, not value ranges (2026-13-45T99:99:99Z passes). Range checks add no value — timestamps are untrusted (D14) and preserved verbatim; server time is authoritative.` No behavior change.

### Finding 4 — `test_unknown_payload_field_survives` doesn't exercise `extra="allow"` — ADDRESS

**Why it's right.** `payload` is typed `dict[str, Any]` on `Body`, so an unknown key inside it round-trips because it's dict data, not because of any model config — the test would pass even if `extra="allow"` were removed. The `body`/`server`/top-level variants do pin the config; this one is mislabeled.

**Fix (minimal).**
- `server/tests/test_envelope.py`: add a comment on `test_unknown_payload_field_survives` clarifying it exercises **dict passthrough**, not the `extra="allow"` config, so a future reader doesn't treat it as the payload-config guard.
- `server/tests/test_payloads.py`: add the **real** payload-config guard — `MessageCreatedV1` also sets `extra="allow"` (for additive-only v1 evolution, §2.3.2). Add `test_message_created_v1_unknown_field_survives`: validate a `MessageCreatedV1` with an unknown key and assert it re-emits under `model_dump` (this is the test that would fail if `extra="allow"` were dropped from the payload model).

### Finding 5 — Headline round-trip is structural, not byte-verbatim — PUSH BACK (document)

**Ruling: structural (dict deep-equal) is the correct invariant; no assertion change.** Reply to post on the thread:

> Deliberate — structural deep-equality is the invariant we want here, not byte/key-order. `event_hash` is SHA-256 over **JCS(body)**, and JCS re-sorts keys canonically, so hash reproducibility depends only on structural fidelity (same keys, same values), which this test asserts. Byte/key-order preservation is explicitly *not* a requirement: `extra="allow"` re-emits unknown fields after declared fields, so declared+extra key order isn't preserved in general — and that's fine because nothing hashes the raw envelope bytes. The "byte-lossless" phrasing in the acceptance criterion means *no field is dropped or mutated* (which `extra="allow"` + str timestamps guarantee), not *byte-verbatim reserialization*.

**Fix (doc only):** add a one-line comment on `test_2_1_example_round_trips_losslessly` noting the assertion is structural-equality-for-JCS-fidelity, not byte-verbatim, so the distinction is captured in the code. No behavior change.

---

### Net change scope

- `server/msgd/core/envelope.py` — rewrite `serialized_size_bytes` to the wire form (Finding 1); add RFC-3339 scope comment (Finding 3).
- `server/tests/test_envelope.py` — add `test_size_cap_is_form_stable`; comment updates on size-cap and round-trip tests (Findings 1, 4, 5).
- `server/tests/test_payloads.py` — add `test_message_created_v1_unknown_field_survives`; assert `format` defaults to markdown (Findings 4, 2).
- No change to `format` field, RFC-3339 regex behavior, or the round-trip assertion.

Only Finding 1 is behavior-changing; the rest are tests/comments/rulings. Re-run local gates (`ruff`, `mypy`, `pytest`) before pushing the fixup. After push, reply on Finding 5's thread with the text above and resolve threads 2/3 with the recorded rulings.
