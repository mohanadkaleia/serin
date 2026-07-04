# ENG-55 — M0: RFC 8785 (JCS) canonicalization in `core/`

- **Linear:** [ENG-55](https://linear.app/kurras/issue/ENG-55/m0-rfc-8785-jcs-canonicalization-in-core) · Milestone M0 — Protocol spike · Priority High
- **Branch:** `mohanad/eng-55-m0-rfc-8785-jcs-canonicalization-in-core`
- **TDD refs:** §2.1 (envelope / example body), §4.1 (stack — `rfc8785` or vendored JCS), D1
- **Implementer:** `python-engineer` (all work is `server/`)
- **Parallel work:** ENG-54 (envelope + schemas) is in flight and also lives in `core/`. This plan touches **no other `core/` module** and makes **zero edits** to `core/__init__.py` to stay merge-clean.

---

## 1. Goal (restated)

Provide the canonical-JSON byte layer that `event_hash` (ENG-56) is computed over. D1 fixes the
scheme as **RFC 8785 (JCS)**. Deliver a single swappable entrypoint `canonicalize(obj) -> bytes`
in `server/msgd/core/jcs.py`, backed by a deliberate library-vs-vendor decision, with edge-case and
property tests that pass the RFC 8785 appendix vectors and the §2.1 example body.

This ticket produces **only** canonicalization. Hashing (`sha256:` over these bytes) is ENG-56;
this module returns bytes and nothing else.

---

## 2. Decision: use the `rfc8785` PyPI package (library route), not a vendored implementation

**Chosen: depend on `rfc8785` (Trail of Bits), wrapped behind our own `canonicalize()`.**

Evaluation of the candidate package (`rfc8785`, source `github.com/trailofbits/rfc8785.py`):

| Criterion | Finding |
|---|---|
| Maintainer / trust | Trail of Bits (security firm); Apache-2.0 (compatible with our use). |
| Dependencies | **Zero runtime deps, pure-Python** — satisfies the `core/` "import-light, no server-only deps" constraint in `core/__init__.py`. |
| Typing | **Ships `src/rfc8785/py.typed`** and is fully annotated → works under our `mypy --strict`. |
| API | `rfc8785.dumps(obj) -> bytes` (UTF-8), `rfc8785.dump(obj, sink)`. Accepts `dict/list/str/int/float/bool/None` (and `tuple`→list). Exactly our shape. |
| Correctness vs RFC | Test suite takes `numgen.go` **and the reference implementation's precomputed inputs + known answers verbatim** (`arrays/french/structures/unicode/values/weird` under `test/assets/`), i.e. it is validated against the RFC/reference vectors including the 100M-case ES6 number set — the single hardest part to get right. |
| Number handling | Implements the ES6 `Number::toString` serialization internally (`_serialize_number`, exponent normalization). |
| Domain enforcement | Raises `IntegerDomainError` outside `[-(2**53)+1, 2**53-1]` (RFC interop cap) and `FloatDomainError` for NaN/Inf; both subclass `CanonicalizationError(ValueError)`. `-0` handled (comment: Python has no +0/-0 distinction; emits `0`). |
| Maturity | v0.1.4 (Sep 2024), Python ≥3.8; small project (~12 stars, single-org maintainer). |

**Why not vendor ~100 lines:** the risky ~80% of a JCS implementation is the ES6 float/number
formatting (Ryu-style shortest round-trip + exponent rules). Re-deriving that for M0 buys nothing
and adds a correctness liability; the library already validates it against the reference 100M-case
number set. The only real downside of the library — a small, low-activity project — is fully
neutralized by (a) our own wrapper isolating the dependency and (b) our own vector/property tests
acting as an independent correctness gate. If the package ever bit-rots, we swap the body of
`canonicalize()` for a vendored implementation **without touching any caller or test** — which is
precisely the swappability the ticket asks for.

**Swappability mechanism:** callers depend only on `msgd.core.jcs.canonicalize` and a local
`JCSError`. The library's exception types are caught inside the wrapper and re-raised as our own
`JCSError`, so no caller (ENG-56 hashing, ENG-54 envelope, `msgctl verify`) ever imports or catches
`rfc8785.*`. The library name appears in exactly one `import` line.

Record the rationale as a module docstring in `jcs.py` (ticket: "record why in the code").

---

## 3. Semantics pinned by this ticket

These are the contract this module guarantees; encode them in the docstring and lock them with tests.

1. **Public signature:** `canonicalize(obj: JSONValue) -> bytes`.
   - The ticket text says `canonicalize(body: dict)`; the parent task and the round-trip AC need
     arbitrary JSON values. Reconcile by accepting **any JSON value** (`obj`), which is a strict
     superset. The production call site (ENG-56) passes the `body` **dict**; document that.
   - `JSONValue` = recursive alias
     `Mapping[str, JSONValue] | Sequence[JSONValue] | str | int | float | bool | None`
     (mypy 1.11 supports recursive aliases; strings are `str`, not treated as sequences here).
2. **Input domain:** `dict` / `list` / `str` / `int` / `float` / `bool` / `None` only, with **string
   object keys**. Anything else (`bytes`, `Decimal`, `datetime`, `set`, custom objects) is rejected
   with a clear `JCSError`. (Implementation: let the library reject and wrap the error, plus one of
   our own tests asserting the message is actionable.)
3. **Floats:** serialized per RFC 8785 = ECMAScript `Number::toString` (delegated to the library).
4. **NaN / Infinity:** **rejected** → `JCSError` (library `FloatDomainError`).
5. **Integer range:** **adopt the RFC 8785 interop cap** `[-(2**53)+1, 2**53-1]`; values outside
   raise `JCSError` (library `IntegerDomainError`). We do **not** attempt Python-bigint support.
   Justification (documented in the module): the hashed `body` only ever carries JSON-safe values —
   every ID is a ULID **string** (`u_`/`s_`/`m_`/… prefix, §2.1), `type_version` is a small int, and
   all counts/sizes/sequences live in `server` metadata which is **not** part of `body` and never
   canonicalized. So the cap is unreachable in practice and gives us bit-for-bit interop with any
   other-language JCS implementation (the web client, §1.1/§12) for free.
6. **Strings:** output is **UTF-8 bytes**; JCS escaping and UTF-16-code-unit key ordering are handled
   by the library. `-0` (int or float) canonicalizes to `0`.
7. **Determinism:** identical input → identical bytes across runs/platforms (no dict-ordering or
   locale sensitivity).

---

## 4. Files

Exactly these, nothing else:

| File | Action | Owner |
|---|---|---|
| `server/msgd/core/jcs.py` | **create** — `JSONValue` alias, `JCSError`, `canonicalize()`, rationale docstring | this ticket |
| `server/tests/test_jcs.py` | **create** — appendix vectors, §2.1 fixture, edge cases, property test | this ticket |
| `server/pyproject.toml` | **edit** — add `rfc8785>=0.1.4,<0.2` to `dependencies` | this ticket |
| `pyproject.toml` (root) | **edit** — add `hypothesis>=6` to `[dependency-groups].dev` | this ticket |
| `uv.lock` | **regenerate** via `uv lock` | this ticket |

**Explicitly NOT touched:**
- `server/msgd/core/__init__.py` — **zero edits** (its docstring already announces `jcs`; no
  re-export line, to avoid a merge conflict with ENG-54 which also edits `core/`). Callers use
  `from msgd.core.jcs import canonicalize`.
- No `core/testdata/vectors.json`. The shared **cross-language** vector file (§1.1/§12) is a hashing
  concern the web client must match bit-for-bit → belongs to **ENG-56** (see Open Questions). ENG-55
  keeps its appendix vectors **inline in `test_jcs.py`** so it adds no third source file.

---

## 5. Step-by-step implementation

**Step 1 — dependencies.**
- Add `rfc8785>=0.1.4,<0.2` to `server/pyproject.toml` `dependencies`.
- Add `hypothesis>=6` to root `pyproject.toml` `[dependency-groups].dev`.
- Run `uv lock` then `uv sync`; confirm `rfc8785` and `hypothesis` resolve.

**Step 2 — `server/msgd/core/jcs.py`.**
- Module docstring: state the scheme (RFC 8785 / D1), the library-vs-vendor decision + rationale
  (§2 above), and the pinned semantics (§3), so the "record why in the code" requirement is met.
- Define `JSONValue` recursive type alias.
- Define `class JCSError(ValueError)` — the module's single, library-agnostic error type.
- Implement:
  ```python
  def canonicalize(obj: JSONValue) -> bytes:
      try:
          return rfc8785.dumps(obj)
      except rfc8785.CanonicalizationError as exc:
          raise JCSError(str(exc)) from exc
  ```
  (Catch the library base `CanonicalizationError`, which covers Integer/FloatDomainError and
  unsupported-type errors, so every failure surfaces as `JCSError`.)
- Keep it import-light: only `rfc8785` + `typing`. No FastAPI/SQLAlchemy.
- mypy strict clean; if the recursive alias needs a nudge, a narrowly-scoped annotation is fine but
  avoid blanket `Any`.

**Step 3 — `server/tests/test_jcs.py`.** See §6.

**Step 4 — quality gates.** `uv run pytest server/tests/test_jcs.py`, `uv run mypy`,
`uv run ruff check` all green.

---

## 6. Test plan (pytest + hypothesis)

**A. RFC 8785 appendix vectors (acceptance).**
- Inline the RFC 8785 **Appendix B** worked example: its input object (mixed unicode incl. the
  `"€"`/Cyrillic strings, nested arrays, the number set) and its expected canonical output bytes,
  verbatim from the RFC. Assert `canonicalize(input) == expected_bytes`.
- Inline the RFC's **ES6 number-formatting samples** as an explicit `(value, expected_str)` table
  (e.g. `0`, `-0`, `1`, `-1`, integers up to `2**53-1`, `0.1`, `1e30`, `1e-7`, `9.999e22`,
  `1e21`, small subnormals) and assert each serializes to the RFC-specified string. This is the
  highest-signal correctness check.

**B. §2.1 example body fixture (acceptance — deterministic output).**
- Build the exact `body` object from TDD §2.1 (the `message.created` envelope's `body`: `event_id`,
  `workspace_id`, `stream_id`, `type`, `type_version`, `author_user_id`, `author_device_id`,
  `client_created_at`, and the `payload` with `message_id`/`text`/`format`/`thread_root_id: null`/
  `file_ids: []`/`mentions: [...]`).
- Assert `canonicalize(body)` is deterministic: stable across repeated calls, and invariant to input
  key insertion order (feed a shuffled-key copy → identical bytes). Snapshot the exact byte string in
  the test so any accidental drift fails loudly (this byte string is the value ENG-56 will hash and
  freeze as a vector).

**C. Edge-case unit tests (ticket-required coverage).**
- **Key ordering:** unsorted keys, keys differing only by case, keys needing UTF-16 code-unit
  ordering (BMP vs astral) → correct JCS order.
- **Nested structures:** deep dict/array nesting, empty `{}`/`[]`, arrays of mixed types.
- **Unicode:** astral-plane characters (e.g. emoji, `U+1D11E`); normalization-sensitive strings
  (composed vs decomposed, e.g. `"é"` NFC vs NFD) — assert JCS does **not** normalize (bytes differ),
  documenting that NFC is the client's responsibility, not JCS's; control chars → `\uXXXX` escapes.
- **Numbers:** integers, floats, exponents, `-0` (int and float → `0`), `2**53-1` (accepted) and
  `2**53` (→ `JCSError`), and the ES6 formatting cases from (A).
- **Escapes:** `"`, `\`, `\n`, `\t`, `\b`, `\f`, `\r`, and `<0x20` control chars.
- **null / bool:** `None`→`null`, `True`/`False`→`true`/`false`, and that `bool` is not confused with
  `int` (`True` ≠ `1` in output).
- **Rejections:** NaN, `+inf`, `-inf`, `2**53`, and non-JSON inputs (`bytes`, `Decimal`, `set`,
  `datetime`, non-string dict key) each raise `JCSError` (not a raw library error).

**D. Property test (acceptance — round-trip idempotence).**
- Hypothesis strategy `json_values`: recursive over `none | booleans | integers(min=-(2**53)+1,
  max=2**53-1) | floats(allow_nan=False, allow_infinity=False) | text() | lists(...) |
  dictionaries(keys=text(), values=...)`. Constrain to the in-domain types only (no NaN/Inf, ints in
  the interop cap) so the strategy never generates a value the module legitimately rejects.
- Assert `canonicalize(json.loads(canonicalize(x))) == canonicalize(x)` for all generated `x`
  (`parse` = `json.loads` on the UTF-8 bytes). This holds even where JSON round-trip collapses
  `float(2.0)`→`int 2` or `-0.0`→`0`, because the assertion compares **output bytes**, which are
  canonical and therefore stable under re-parse.

---

## 7. Risks / open questions

1. **Library longevity (small project, last release Sep 2024).** Mitigated by the `canonicalize()`
   wrapper + our independent appendix/property tests; a future swap to a vendored impl is a
   single-function change behind a stable public surface. Low residual risk.
2. **Integer interop cap could surprise a future payload.** If any `body` field ever needs an int
   `> 2**53-1`, `canonicalize` raises. Mitigated: documented in the module, and ENG-54 payload
   schemas keep numerics small / IDs as strings. A boundary test (`2**53-1` ok, `2**53` rejects)
   pins the behavior so any future regression is caught.
3. **mypy strict + recursive `JSONValue` alias / library private types.** Low risk (library ships
   `py.typed`); if the alias is awkward, use a minimal local annotation rather than `Any`.
4. **`uv.lock` merge contention with ENG-54.** Both tickets add deps and regenerate `uv.lock`.
   Resolution: whoever merges second re-runs `uv lock`; lockfile conflicts are mechanical, not
   semantic. Flag in the PR.
5. **`core/__init__.py` contention with ENG-54.** Avoided entirely by making zero edits there.
6. **Open question — cross-language `core/testdata/vectors.json` (§1.1/§12).** The shared vector file
   that the web-client JCS/hash reimplementation must also pass is fundamentally a **hashing** artifact
   (bytes → `sha256:` digest). Recommend it be **owned by ENG-56**, seeded from ENG-55's §2.1 snapshot
   and appendix cases. ENG-55 deliberately does not create it, to respect this ticket's file scope.
   Confirm this split with ENG-56's plan.

---

## 8. Acceptance criteria mapping

| AC | Covered by |
|---|---|
| Passes RFC 8785 appendix test data | §6.A |
| Deterministic output for §2.1 example body | §6.B |
| Property: `canonicalize(parse(canonicalize(x))) == canonicalize(x)` | §6.D |
| Edge cases (ordering/nesting/unicode/numbers/escapes/null/bool) | §6.C |
| Single swappable `canonicalize(obj) -> bytes` | §2, §5 Step 2 |
