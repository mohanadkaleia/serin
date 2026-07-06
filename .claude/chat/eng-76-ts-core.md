# ENG-76 — M2: TypeScript core — envelope + JCS + hashing passing the frozen cross-language vectors

- **Linear:** ENG-76 · Milestone M2 — Web client · Priority High
- **TDD refs:** §1.1 (web client re-implements envelope+hashing; both must pass the shared vectors in `core/testdata/`), §2.1 (envelope, `event_hash` = SHA-256 over JCS(body) only, MAX_DEPTH=128, ±(2^53−1) integer cap), §5.1/§5.3 (SharedWorker send path builds envelope + hash), D1
- **Implementer:** `ui-engineer` (all work is `web/`)
- **Depends on (merged to main):** ENG-55 (`jcs.py` semantics), ENG-56 (the FROZEN `server/msgd/core/testdata/vectors.json`), ENG-75 (`web/` scaffold — Vite/Vue3/TS-strict/Pinia/Tailwind, pnpm, vitest, web CI job; `src/core/` is a documented seam awaiting this ticket).
- **This is the load-bearing cross-language proof:** the M0 exit artifact (`vectors.json`) is a contract; ENG-76 is the second, genuinely-independent implementation that must reproduce it **byte-for-byte**. If the TS port and the Python reference disagree on one byte, the whole "one protocol, two languages" claim fails here.

---

## 1. Goal (restated)

A TypeScript port, in `web/src/core/`, of the four Python `core/` layers the browser send path needs:

1. **JCS canonicalization** (`jcs.ts`) — RFC 8785, mirroring `jcs.py`'s locked semantics (MAX_DEPTH=128, ±(2^53−1) integer cap, NaN/Infinity reject, −0→0, lone-surrogate reject, no NFC normalization, UTF-16 code-unit key sort).
2. **SHA-256 hashing** (`hashing.ts`) — `event_hash = "sha256:" + hex(sha256(JCS(body)))`, **async** (WebCrypto `crypto.subtle`).
3. **Typed-ULID** (`ids.ts`) — monotonic mint (CSPRNG), validate, parse; the 6 entity prefixes + bare `event_id`.
4. **The `message.created` body builder** (`envelope.ts` + `payloads/`) — the TS mirror of `build_message_created_body`.

**Acceptance core:** a vitest runner (`vectors.spec.ts`) that loads the **one frozen** `server/msgd/core/testdata/vectors.json` (never a copy) and passes every case — valid cases byte-for-byte on `canonical_b64` and on `hash`, error cases asserting no hash is produced.

Scope is the send/hash path only. Read-side projections, Dexie, the worker, and non-`message.created` payloads are later M2 tickets (ENG-77+). This ticket ships the crypto/canonicalization spine those depend on.

---

## 2. Crux ruling #1 — JCS: **hand-port**, not an npm library (the inverse of the Python decision)

**Chosen: hand-port `canonicalize()` in `jcs.ts`, leaning on two JS-native primitives.** Python vendored the `rfc8785` package *because ES6 number formatting was the hard 80%*. In JavaScript that 80% is **free and native**, which flips the calculus.

Verified against `node` during planning (all match the frozen vectors exactly):

| JCS requirement | JS-native mechanism | Verified |
|---|---|---|
| ES6 `Number::toString` number production | **`String(n)`** for finite `n` | `String(1e30)`→`"1e+30"`, `String(1e21)`→`"1e+21"`, `String(9.999e22)`→`"9.999e+22"`, `String(5e-324)`→`"5e-324"`, `String(1e-7)`→`"1e-7"`, `String(0.1)`→`"0.1"`, `String(2.0)`→`"2"` |
| −0 → 0 | **`String(-0)`** → `"0"` | native; no special case needed |
| UTF-16 code-unit key ordering | **`keys.sort()`** (Array default compares strings by UTF-16 code unit) | `keys-utf16-astral` vector: emoji lead surrogate `0xD83D` sorts before `0xFFFF` — native `.sort()` gets this right |

**Why not the `canonicalize` npm package (Samuel Erdtman, an RFC 8785 co-author) or any other lib:** it would buy us *nothing* the platform doesn't already give us (number formatting, key sort), and it does **not** enforce any of the msg-specific domain rules we are actually on the hook for — MAX_DEPTH=128, the ±(2^53−1) integer cap, NaN/Infinity rejection, lone-surrogate rejection. The reference impl emits `null` for NaN/Infinity and recurses (a stack-overflow liability on the 2000-deep pathological vector). So we would wrap a dependency **and still hand-write every domain check** — strictly more surface (supply chain + our checks) than writing the ~70-line canonicalizer ourselves. Record this rationale as the `jcs.ts` module docstring, mirroring how `jcs.py` records the opposite ruling.

**What we hand-write (all small, all pinned by vectors):**
- Iterative depth pre-pass (§2.2), string escaping table (§2.3), lone-surrogate reject (§2.4), the recursive value serializer (object → sorted `{...}`, array → order-preserving `[...]`, string → escaped, number → `String(n)` after finite-check, `true`/`false`/`null`), then `new TextEncoder().encode(str)` → `Uint8Array`.
- Public surface: `canonicalize(value: JSONValue): Uint8Array`, `class JCSError extends Error`, `const MAX_DEPTH = 128`, `type JSONValue`. Mirrors `jcs.py`'s `canonicalize`/`JCSError`/`MAX_DEPTH`/`JSONValue`.

### 2.1 Crux ruling #1a — the integer interop cap is a **parse-time, source-literal** rule (the #1 subtlety)

This is the single hardest cross-language point and must be got right, because the vectors are deliberately adversarial here:

- **Accepted beyond 2^53:** `1e21` (`num-1e21`), `1e30` (`num-exp-large`), `9.999e22` (`num-9999e22`) — all exponential-form → floats → **not** capped.
- **Rejected at 2^53:** `9007199254740992` (`reject-int-over-cap`), `9007199254740993` (`reject-int-over-cap-plus1`) — plain integer literals just over the cap.

`1e21` (≈10²¹) is numerically **larger** than `2^53` (≈9×10¹⁵) yet accepted, while `2^53` is rejected. So the rule is **not** magnitude of the parsed number, and it is **not** `Number.isSafeInteger` (that would reject `1e21`). Python distinguishes them by **source type**: `json.loads("1e21")`→`float` (uncapped ES6 path), `json.loads("9007199254740992")`→`int` (capped). `JSON.parse` erases that distinction *and* silently truncates: verified `JSON.parse("9007199254740993")` → `9007199254740992`. So the cap **cannot** be enforced on the parsed `number`.

**Ruling: enforce the cap at the JSON-parse boundary using source-text access.** Provide `parseJcsJson(text: string): JSONValue` in `jcs.ts` (the TS equivalent of the wire path's `json.loads`) implemented as:

```ts
JSON.parse(text, (_key, value, context) => {
  if (typeof value === 'number' && context && /^-?\d+$/.test(context.source)) {
    // integer literal (no '.', 'e', 'E') — apply the ±(2^53−1) interop cap on the
    // ORIGINAL literal via BigInt, immune to JSON.parse's ≥2^53 truncation
    const n = BigInt(context.source)
    if (n > 9007199254740991n || n < -9007199254740991n) throw new JCSError('integer out of interop range')
  }
  return value
})
```

Verified: the reviver's `context.source` is `"9007199254740993"` — the **pre-truncation** literal — so `BigInt(source)` reasons about the true value. Exponential forms (`1e21`, `1e30`) fail the `/^-?\d+$/` integer-literal test → treated as floats → accepted, exactly matching Python's int-vs-float split. `JSON.parse` source-text access is Stage-4 / shipped in V8 12.0 (Node 21+, Chrome 120+), Firefox 133, Safari 18.2 — available in CI's Node 22 (verified locally) and in every evergreen browser the SPA targets. Fallback if browser-support ever regresses: a `String(n)`-form heuristic (reject `Number.isInteger(n) && !Number.isSafeInteger(n) && !String(n).includes('e')`) passes all frozen vectors but is semantically weaker (mis-accepts non-exponential integer literals ≥1e21, which cannot occur in a real body and are not in the frozen suite) — documented as the fallback, not the default.

**Where the cap lives:** at parse, in `parseJcsJson`, **not** in `canonicalize`. This matches the physical reality that JS loses the information after parse, and the vectors' `_meta` explicitly makes error `stage` "a hint, not a hard assertion" — so a parse-stage rejection is fully conformant. `canonicalize` itself only guards `Number.isFinite` (defense for programmatically-constructed values; NaN/Infinity can't survive `JSON.parse` anyway — verified `JSON.parse("NaN")` throws `SyntaxError`). Real send-path bodies are built programmatically with tiny numbers (`type_version:1`, everything else strings), so the cap never fires in production — it exists solely to reject adversarial parsed input, and parse is the correct seam.

### 2.2 Depth guard — iterative, runs first (mirrors the ENG-55 security fix)

`_checkDepth(value)` = explicit worklist of `[value, depth]` pairs, **never recursive** (the guard must be immune to the stack exhaustion it guards). Descends only into object values and array items; scalars/strings are not iterated. Throws `JCSError` when a container level exceeds `MAX_DEPTH=128`. Called **first** in `canonicalize`, before the recursive serializer, so the `reject-depth-pathological` vector (2000-deep) rejects cleanly with no `RangeError: Maximum call stack size exceeded` escaping. Depth counting matches `jcs.py`: scalar depth 0, `{}`/`[]` depth 1, the §2.1 body depth 3; `depth-at-cap-*` (exactly 128) accepted, `reject-depth-over-cap-*` (129) rejected.

### 2.3 String escaping table (RFC 8785 §3.2.2.2)

`"` → `\"`, `\` → `\\`; `\b`(0x08) `\t`(0x09) `\n`(0x0A) `\f`(0x0C) `\r`(0x0D) → short escapes; other control chars `< 0x20` → lowercase `\u00XX`. **Everything else emitted raw** — including `0x7f` (DEL is **not** escaped; the `raw-0x7f` vector is the concrete reason `canonical_b64` cannot be a JSON string) and all non-ASCII (raw UTF-8 via `TextEncoder`, per `unicode-bmp`/`unicode-astral`). Pinned by `escapes-short`, `escapes-control`, `raw-0x7f`. No NFC normalization — `unicode-nfc` and `unicode-nfd` are distinct hashes (JCS never normalizes; NFC is the client's responsibility, per D1).

### 2.4 Lone-surrogate rejection (both keys and values)

`JSON.parse('{"\ud800":1}')` does **not** throw — JS strings permit lone surrogates. And `TextEncoder.encode` silently replaces them with U+FFFD rather than throwing, so we **cannot** rely on the encoder. Detect explicitly: for every string (object key and string value) check **`str.isWellFormed()`** (ES2024; Node 20+, evergreen browsers — verified `"\ud800".isWellFormed()` === `false`) and throw `JCSError` if false. Fallback: `/\p{Surrogate}/u` test or a manual code-unit scan. Pins `reject-surrogate-key` and `reject-surrogate-value`.

### 2.5 Input domain / `JSONValue`

```ts
export type JSONValue =
  | { [key: string]: JSONValue } | JSONValue[]
  | string | number | boolean | null
```

`canonicalize` rejects anything outside this (`undefined`, `bigint`, `symbol`, `function`, `Date`, class instances) with `JCSError`. **`bigint` is deliberately NOT in the domain** — numbers are IEEE-754 f64; the ±(2^53−1) cap *is* the boundary and is enforced at parse (§2.1), so no bigint ever legitimately reaches `canonicalize`.

---

## 3. Crux ruling #2 — hashing is **async** (`crypto.subtle`)

WebCrypto's digest is Promise-based, so the API is async — unlike Python's sync `hash_event`:

```ts
export const HASH_ALGORITHM = 'sha256'
export async function hashEvent(body: JSONValue): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', canonicalize(body))
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
  return `sha256:${hex}`
}
```

**Downstream implication (benign):** §5.3's optimistic send path already `await`s envelope+hash construction in the worker before enqueuing the outbox — an async hash fits with no architectural change. Callers `await hashEvent(...)`; the vector runner awaits it. Propagates `JCSError` for out-of-domain input (does not swallow), mirroring `hashing.py`. `verifyHash` is **not** required for ENG-76 (no upload/verify path in the web client yet); defer it to the receive-path ticket to avoid inventing an unused surface. Public surface: `HASH_ALGORITHM`, `hashEvent`.

---

## 4. Crux ruling #3 — the vector runner imports the **one frozen file**, no copy

**Ruling: read `server/msgd/core/testdata/vectors.json` at test time via `fs`, resolved relative to the spec file.** There is exactly one frozen file in the repo; the TS runner and the Python runner prove the *same bytes* — a copy could drift and silently break the cross-language guarantee.

```ts
// web/tests/unit/vectors.spec.ts
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
const vectorsPath = fileURLToPath(
  new URL('../../../server/msgd/core/testdata/vectors.json', import.meta.url),
)
const suite = JSON.parse(readFileSync(vectorsPath, 'utf8'))
```

`../../../` climbs `web/tests/unit → web/tests → web → repo root`, then into `server/…`. **Why `fs.readFileSync`, not a static `import`:** a static `import … from '../../../server/…/vectors.json'` would (a) drag a file outside `web/`'s `tsconfig.app.json` `include` into vue-tsc's type graph (rootDir/`resolveJsonModule` friction) and (b) couple the bundler's module resolution to a cross-tree path. `fs` at runtime keeps the frozen file entirely out of the TS/bundler graph while still being the *same* bytes on disk. It works in CI because the `web` job does a full `actions/checkout` of the whole repo, so `server/…/vectors.json` is present in the workspace (the job only `cd`s to `web/` as `working-directory`; absolute-path resolution via `import.meta.url` is cwd-independent). `import.meta.url` is available (ESM; `"type":"module"`).

**Runner logic**, parametrized by `case.id`:
- **Valid cases:** `const value = parseJcsJson(c.input_json)`; assert `canonicalize(value)` equals `Buffer.from(c.canonical_b64, 'base64')` (byte-for-byte — isolates JCS bugs from hash bugs); then `expect(await hashEvent(value)).toBe(c.hash)`.
- **Error cases (`c.error` present):** assert the whole `parse → canonicalize → hash` pipeline **throws / produces no hash** — `await expect((async () => hashEvent(parseJcsJson(c.input_json)))()).rejects.toThrow()` (wrapping parse too, since some rejects fire at parse: `NaN` at `JSON.parse`, `2^53` at the cap reviver, surrogates/depth at canonicalize). Stage-agnostic, per `_meta`.
- **Anchor assertion:** additionally assert the `tdd-2.1-example` case hash equals the hardcoded `sha256:49d43880190e9b17c2b4eb5cd4fbe39c972ba0d214b3f751d6033cb0fd707e51`, so the TS suite is not purely file-relative (independent pin identical to ENG-56's).
- **Meta assertions:** `_meta.max_depth === MAX_DEPTH` (128) and `_meta.int_interop_cap` deep-equals `[-9007199254740991, 9007199254740991]`, so a future TS drift from the frozen constants fails loudly.

---

## 5. Crux ruling #4 — typed-ULID hand-port (mirrors `ids.py`)

Hand-port `ids.ts` (no `ulidx` dependency — we need typed prefixes + validation the lib doesn't provide, and monotonic parity with `ids.py`; the base32 encode is ~15 lines). 128-bit ULID = 48-bit big-endian ms timestamp + 80-bit randomness, Crockford base32 (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`) → 26 chars.

- **Randomness:** `crypto.getRandomValues(new Uint8Array(10))` — the CSPRNG requirement that `ids.py` gets from `secrets`. **Not** `Math.random`.
- **Monotonic within a millisecond:** module-level `lastMs` / `lastRandomness` (a `bigint` for the 80-bit value); same-ms mint increments `lastRandomness` so `newUlid() < newUlid()` always holds lexicographically; on 80-bit overflow carry into `lastMs`. **No mutex needed** — a `SharedWorker` (and any single JS realm) is single-threaded, so `ids.py`'s `threading.Lock` has no counterpart (note this divergence in the docstring). Matches `ids.py`'s monotonic-within-ms semantics.
- **Prefixes:** `IdKind` = `w_ u_ s_ m_ f_ d_`; `event_id` is a **bare** ULID (no prefix, §2.1).
- **Surface (mirrors `ids.py`):** `newUlid`, `newEventId`, `newTypedId(prefix)`, `newWorkspaceId`/`newUserId`/`newStreamId`/`newMessageId`/`newFileId`/`newDeviceId`, `isValidUlid`, `isValidTypedId(value, prefix)`, `parseTypedId(value, expectedPrefix?)`, `ENTITY_PREFIXES`. `isValidUlid` = 26 chars, all in the Crockford alphabet, first char ≤ `'7'` (48-bit timestamp overflow guard, matching a correct ULID decoder).

---

## 6. The `message.created` body builder (`envelope.ts` + `payloads/`)

Mirror `build_message_created_body` (payloads/`__init__.py`) and `MessageCreatedV1` (payloads/message.py):

- `web/src/core/payloads/message.ts` — a `MessageCreatedV1` payload TS type + a `buildMessageCreatedPayload(...)` that mints `message_id` when absent and format-validates id fields (`m_`/`u_`/`f_` prefixes via `ids.ts`), `format ∈ {markdown, plain}`, `thread_root_id` nullable. Field order and defaults (`thread_root_id: null`, `file_ids: []`, `mentions: []`, `format: "markdown"`) match the Python model — the builder's output must be a plain object whose JCS canonicalization equals the reference.
- `web/src/core/envelope.ts` — `type Body`, and `buildMessageCreatedBody({...}): Body` minting `event_id` when absent, assembling the §2.1 body shape. Plus a small `finalizeEnvelope(body): Promise<{body, event_hash}>` helper (`event_hash = await hashEvent(body)`) producing the §3.2 wire form `{ body, event_hash }` the send path enqueues. Object insertion order is irrelevant to the hash (canonicalize sorts keys), so the builder need not match key order — but a unit test pins that a builder-produced body with the §2.1 fixed ids/text/timestamp hashes to the anchor.
- Scope note: only `message.created` is built here — it is the only event type the web client *emits* in the M2 send path (§5.3). Other payload types are read-side projection concerns for later tickets; do not port them now.

---

## 7. Files

**Create — `web/src/core/`:**

| File | Contents |
|---|---|
| `jcs.ts` | `JSONValue`, `JCSError`, `MAX_DEPTH=128`, `canonicalize(value): Uint8Array`, `parseJcsJson(text): JSONValue` (cap-aware, §2.1); module docstring records the hand-port-vs-lib ruling (§2) |
| `hashing.ts` | `HASH_ALGORITHM`, `async hashEvent(body): Promise<string>` (§3) |
| `ids.ts` | typed-ULID mint/validate/parse, `IdKind`, `ENTITY_PREFIXES` (§5) |
| `payloads/message.ts` | `MessageCreatedV1` type + `buildMessageCreatedPayload` (§6) |
| `envelope.ts` | `Body` type, `buildMessageCreatedBody`, `finalizeEnvelope` (§6) |
| `index.ts` | barrel: re-export `canonicalize`/`JCSError`/`MAX_DEPTH`/`JSONValue`/`parseJcsJson`, `hashEvent`/`HASH_ALGORITHM`, ids surface, `buildMessageCreatedBody`/`finalizeEnvelope` |

**Create — `web/tests/unit/`:**

| File | Contents |
|---|---|
| `vectors.spec.ts` | **the acceptance runner** (§4) — `// @vitest-environment node` at top (§8); fs-reads the frozen file; every case; anchor + `_meta` pins |
| `jcs.spec.ts` | focused `(input, expectedString)` number table (1e30→`1e+30`, 1e21→`1e+21`, 9.999e22→`9.999e+22`, 5e-324, 1e-7, 0.1, 2.0→`2`, −0→`0`, 2^53−1); key-sort (case + UTF-16 astral); escaping; domain rejects (`bigint`, `undefined`, non-string key). `// @vitest-environment node` (uses TextEncoder — fine either env, but keep consistent) |
| `hashing.spec.ts` | hash shape `^sha256:[0-9a-f]{64}$`, determinism, anchor hash. `// @vitest-environment node` (§8) |
| `ids.spec.ts` | ULID format, monotonic `newUlid() < newUlid()` across a same-ms burst, prefix validate/parse, `event_id` bare |
| `envelope.spec.ts` | builder with §2.1 fixed ids/text/timestamp → `finalizeEnvelope` → `event_hash` equals the anchor; payload id-validation rejects |

**Edit:** none required. `vite.config.ts` test block already collects `tests/unit/**/*.spec.ts`; per-file `// @vitest-environment node` (§8) avoids touching shared config. `.github/workflows/ci.yml` web job already runs `pnpm test`/`pnpm typecheck`/`pnpm build`/`pnpm lint`/`pnpm format:check` — the new files are collected automatically (§9). No new dependencies (WebCrypto, TextEncoder, BigInt, `fs` are platform/Node built-ins).

**Explicitly NOT touched:** `src/worker/` (ENG-77), any Vue/UI, Dexie, `package.json` deps.

---

## 8. Crux ruling #5 — SubtleCrypto in the test env → run core specs under **node**

`crypto.subtle` is present in browsers and in Node's global `crypto` (webcrypto, Node ≥20). The web project's vitest default `environment: 'jsdom'` (needed for Vue component tests) does **not** reliably expose `crypto.subtle` (jsdom implements `getRandomValues` but not `SubtleCrypto`). **Ruling: mark every core spec that touches crypto with a per-file `// @vitest-environment node` docblock** (`vectors.spec.ts`, `hashing.spec.ts`, `envelope.spec.ts`, and for consistency `ids.spec.ts`/`jcs.spec.ts`). Node's `environment` gives the webcrypto global, `Buffer` (base64 decode), `fs`, and `TextEncoder`. This is a per-file annotation — zero change to `vite.config.ts`, and the Vue/jsdom tests keep the default. Production code references `crypto.subtle` / `crypto.getRandomValues` via the platform global (identical name in browser and Node), so no env-shim leaks into `src/`.

---

## 9. CI wiring

The `web` CI job (ci.yml, `checks`' sibling) already runs `pnpm lint → format:check → typecheck → test → build` from `working-directory: web`. What ENG-76 must ensure:

1. **The cross-repo file resolves in CI.** The job's `actions/checkout` pulls the whole monorepo, so `../server/msgd/core/testdata/vectors.json` exists relative to `web/`. `vectors.spec.ts` resolves it via `import.meta.url` (cwd-independent). No workflow edit needed — but the plan flags: if a future CI change ever narrows the web job to a sparse/partial checkout of only `web/`, the vector runner breaks. Add a comment in `vectors.spec.ts` asserting the file's presence with an actionable error if missing.
2. **`pnpm typecheck` (vue-tsc, strict) stays green.** The `web/` tsconfig is strict with `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `verbatimModuleSyntax`, `isolatedModules`, `noUnusedLocals`. Implications for the port: use `import type` for type-only imports (`verbatimModuleSyntax`); guard array/record indexing (`noUncheckedIndexedAccess`) in the base32 codec and depth worklist; optional builder params typed precisely (`exactOptionalPropertyTypes`). The frozen JSON is fs-read (not imported), so it never enters the type graph.
3. **`pnpm format:check` (prettier) and `pnpm lint` (eslint).** New files must be prettier-clean and eslint-clean before push.
4. **`pnpm test` collects `vectors.spec.ts`** automatically (glob match). No config change.

No changes to the Python `checks`/`image` jobs; ENG-76 is entirely inside the `web` job's existing pipeline.

---

## 10. Step-by-step (all `ui-engineer`)

1. **`jcs.ts`** — `JSONValue`, `JCSError`, `MAX_DEPTH`; iterative `_checkDepth`; recursive serializer (object sort + escape + `String(n)` finite-checked number + literals); lone-surrogate `isWellFormed` guard; `canonicalize`; `parseJcsJson` cap-aware reviver (§2.1). Module docstring = hand-port ruling.
2. **`hashing.ts`** — async `hashEvent` (§3).
3. **`ids.ts`** — Crockford base32 codec, monotonic mint, validators/parsers (§5).
4. **`payloads/message.ts` + `envelope.ts`** — payload + body builders (§6).
5. **`index.ts`** — barrel.
6. **`vectors.spec.ts`** — the runner (§4) with `// @vitest-environment node`. Get every case green — this is the acceptance gate.
7. **`jcs.spec.ts` / `hashing.spec.ts` / `ids.spec.ts` / `envelope.spec.ts`** — focused unit tests (§7), especially the number `(input, expected)` table (fast diagnosis if a number case regresses) and the builder→anchor cross-check.
8. **Quality gates:** `pnpm typecheck && pnpm lint && pnpm format:check && pnpm test && pnpm build` all green locally, then push (the `web` CI job runs the same).

---

## 11. Risks / open questions

1. **Integer interop cap (highest risk).** `1e21` accepted but `2^53` rejected is *not* a magnitude rule and `JSON.parse` truncates ≥2^53 — enforceable only on the source literal (§2.1). Mitigation: source-text-access reviver (verified available in Node 22 + evergreen browsers), with the `String(n)`-form heuristic as documented fallback. The `reject-int-over-cap*` vectors are the guard.
2. **SubtleCrypto availability in vitest.** jsdom lacks `crypto.subtle`. Mitigation: per-file `// @vitest-environment node` (§8); verified Node exposes webcrypto globally.
3. **Lone surrogates don't throw.** Neither `JSON.parse` nor `TextEncoder` rejects them. Mitigation: explicit `isWellFormed()` check on every key and string value (§2.4); `reject-surrogate-*` vectors guard it.
4. **Iterative depth guard ordering.** A recursive canonicalizer would stack-overflow on the 2000-deep `reject-depth-pathological` vector before any depth check. Mitigation: iterative `_checkDepth` runs first (§2.2); the vector proves no `RangeError` escapes.
5. **Number formatting is native but still pinned.** `String(n)` = ES6 `Number::toString` is verified for every vector number case, so this is LOW risk in TS (the inverse of Python) — but the `jcs.spec.ts` `(input, expected)` table and the vector number cases (1e30, 5e-324, −0, 2^53 boundary, 1e21) are kept as explicit guards against a future engine/typo regression, per the ticket's call-out.
6. **Cross-repo file path fragility.** `../../../server/…` depends on a full-repo checkout in the `web` CI job (currently true). Mitigation: resolve via `import.meta.url`, add a clear "vectors file not found — is this a full checkout?" error (§9.1).
7. **Strict-TS friction.** `verbatimModuleSyntax`/`noUncheckedIndexedAccess`/`exactOptionalPropertyTypes` (§9.2) — routine but the base32 codec and reviver need care. Low risk.
8. **`verifyHash` intentionally omitted.** No web upload/verify path exists yet (ENG-77+). Deferring avoids an unused, mis-usable surface (the Python `verify_hash` carries a "not the upload authority" trap). Flag for the receive-path ticket: verify by re-hashing the raw received body with `hashEvent`, never a re-serialized model — same contract as `hashing.py`.

---

## 12. Acceptance-criteria mapping

| AC | Covered by |
|---|---|
| TS JCS + SHA-256 pass `vectors.json` byte-for-byte (valid: `canonical_b64` + `hash`) | §4 runner, `vectors.spec.ts` |
| Error cases produce no hash | §4 runner (stage-agnostic throw) |
| One frozen file, no copy/drift | §4 fs-read of `server/…/vectors.json` |
| MAX_DEPTH=128 iterative; at-cap accept / over-cap + 2000-deep reject | §2.2, depth vectors |
| ±(2^53−1) integer cap (magnitude-on-literal) | §2.1, `reject-int-over-cap*` + `num-1e21`/`num-exp-large` |
| NaN/Infinity reject; −0→0; no NFC; lone-surrogate reject; UTF-16 key sort; raw 0x7f | §2.1–2.4, corresponding vectors |
| ES6 number formatting | §2 (`String(n)` native) + `jcs.spec.ts` table |
| async `hashEvent` → `"sha256:<hex>"` | §3, `hashing.spec.ts` |
| typed-ULID mint (CSPRNG, monotonic) / validate / parse, 6 prefixes + bare event_id | §5, `ids.spec.ts` |
| `message.created` body builder (mirror of `build_message_created_body`) | §6, `envelope.spec.ts` (builder→anchor) |
| Wired into web CI (`pnpm test`) | §9 |
