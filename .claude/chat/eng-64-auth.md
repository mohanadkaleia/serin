# ENG-64 — M1: Auth (first-run, argon2id login, opaque per-device sessions, single-use invites) — D6

**Ticket:** [ENG-64](https://linear.app/kurras/issue/ENG-64) · Milestone M1 — Sync server · Priority High · assignee Mohanad
**Branch:** `mohanad/eng-64-m1-auth-first-run-registration-argon2id-login-opaque-per`
**Spec:** TDD §7 (auth/D6), §4.2 (users/devices/sessions/invites), §4.3 (rate limits), §3.2 (RFC 9457 problem+json).
**Implementer:** `python-engineer` for everything (all `server/`).

This is the **first request-serving surface** on top of the ENG-63 scaffold. Two things it establishes are load-bearing for every later M1 router (ENG-65+): the **RFC 9457 problem+json error convention** and the **`AuthContext` dependency contract**. Both are specified precisely below because every subsequent endpoint inherits them.

---

## 1. What already exists (ENG-63, merged on `main`) — extend, don't reinvent

- **App factory** `msgd/api/app.py::create_app(settings)` — lifespan installs the async sessionmaker (ping only, no DDL), includes `health.router`. We extend this: register problem+json exception handlers, config-gate docs, include the new routers.
- **DB models** `msgd/db/models.py` — the full §4.2 schema is already defined, including `User`, `Device`, `Session`, `Invite`, `Workspace`, `Stream`, `StreamMember`. **No new tables are needed.** Column shapes we rely on:
  - `Session(token_hash PK, user_id FK, device_id FK, created_at, last_seen_at, expires_at)`
  - `Invite(token_hash PK, workspace_id, created_by, role default 'member', expires_at, used_by nullable)` — `used_by IS NULL` ⇒ unused (single-use guard).
  - `User` — `UNIQUE(workspace_id, email)`, `role CHECK IN (owner,admin,member,guest)`, `deactivated_at nullable`, `password_hash` (argon2id).
  - `Device(device_id PK, user_id FK, label, public_key null, created_at)`.
- **Session dependency** `msgd/db/engine.py::get_session` — request-scoped `AsyncSession`; the harness overrides it to bind to a rolled-back outer transaction. All new DB access goes through `Depends(get_session)`.
- **Settings** `msgd/settings.py` — `MSG_`-prefixed pydantic-settings; `secret_key` is already present (unused until now). We add the auth knobs here.
- **Logging** `msgd/logging.py::JsonFormatter` — merges `extra=` fields into the JSON line. Relevant to the "no raw tokens in logs" requirement (§ Decision 2).
- **IDs** `msgd/core/ids.py` — `new_user_id()`, `new_device_id()`, `new_workspace_id()` (typed ULIDs). Use these for all minted ids.
- **Harness** `server/tests/harness.py` — session-scoped `postgres:17` container, real Alembic migration once, **per-test rollback isolation via savepoint** (`join_transaction_mode="create_savepoint"`), in-process ASGI `client` fixture with `get_session` overridden. Handler `commit()`s land on savepoints and are rolled back per test. **Caveat that matters here:** the `client` fixture routes every request through *one shared connection/session*, so it **cannot exercise true DB concurrency** (advisory-lock race, parallel logins). Concurrency tests need a separate committing fixture — see § Test plan / Risks.

---

## 2. Decisions pinned

### D1 — First-run mechanics → `POST /v1/setup`, advisory-lock race guard
- **Ruling:** explicit `POST /v1/setup {workspace_name, email, password, display_name}`, valid **only while zero users exist**; returns a logged-in session (same shape as login). Any call once ≥1 user exists → **409** problem+json (`type=/problems/already-initialized`). `msgctl init-server` is **out of scope** — it becomes a thin remote wrapper over this endpoint in a later ops ticket; leave that seam (the endpoint is the mechanism).
- **Race-safety ruling:** wrap the setup transaction in a Postgres **transaction-scoped advisory lock** — `SELECT pg_advisory_xact_lock(<fixed constant, e.g. hashtext('msg:setup')>)` as the first statement, then `SELECT count(*) FROM users` guard, then insert workspace + owner. The lock serializes concurrent setups; the loser sees `count > 0` and returns 409. **Zero schema change** (a unique/singleton constraint would require touching the frozen §4.2 schema; advisory lock does not, and is single-worker-friendly). Commit releases the lock.
- **Owner creation:** mint `w_` workspace id + `u_` user id; insert `Workspace(name=workspace_name)` and `User(role='owner', ...)`. Then mint device + session and return the token (auto-login).
- **Seam for ENG-65:** setup creates only the `workspaces` + `users` rows. The `workspace-meta` stream, `workspace.created`, and `user.joined` event emission are **deferred to the streams ticket**. Mark the call site with a clearly-labelled `# ENG-65 seam: emit workspace.created + user.joined here` comment; do not stub a fake stream.
- **Single-workspace assumption (MVP):** one server = one workspace (§7). `count(users)==0` is the whole-server gate. Documented, matches D6.

### D2 — Token discipline
- **Mint:** `secrets.token_urlsafe(32)` → 32 bytes = 256 bits, URL-safe string. This is the raw bearer token.
- **Store:** `token_hash = sha256(raw.encode()).hexdigest()` (hex) → written to `sessions.token_hash` (PK, already indexed). **Raw token is returned once in the response body and never persisted.**
- **Lookup timing:** session/invite lookup is an **exact equality on a PK-indexed sha256 hex** of a 256-bit secret. There is no usable timing surface (an attacker cannot exploit a timing leak on a full high-entropy hash; a partial-prefix attack is meaningless against a hashed value). No `compare_digest` needed on the DB lookup itself.
- **The real timing/enumeration surface is the login argon2 verify path** → **rule dummy-hash-on-unknown-email:** when the email is not found, run `verify_password` against a **module-level precomputed dummy argon2 hash** and then return the *same* generic 401 (`invalid email or password`) as the wrong-password path. Identical status, body, and work regardless of whether the email exists ⇒ no user enumeration by timing or by response shape.
- **Never-logged ruling:** raw tokens and passwords are only ever placed in **response models**, never in a log call, never in `extra=`. Belt-and-suspenders: add a small `logging.Filter` to the logging config that drops any `extra` key in `{token, password, authorization, secret, session_token}`. A dedicated test greps captured log output for the raw token (§ Test plan #8).

### D3 — Device identity
- **Login body extends to** `{email, password, device_label, device_id?}`.
- **No `device_id` presented:** mint server-side (`new_device_id()`, `d_` ULID), insert a `devices` row with the `device_label`.
- **`device_id` presented:** it must **exist and be owned by the authenticating user**. If it does, reuse it (update `label` to the supplied `device_label`; `created_at` immutable). If it is unknown or owned by another user → **400** problem+json (`type=/problems/invalid-device`) — this branch runs only *after* successful password auth, so it discloses nothing about credentials, and `d_` ULIDs aren't meaningfully enumerable.
- **Upsert semantics:** insert-on-mint, update-label-on-reuse. One `devices` row per physical device; sessions reference it.

### D4 — Session model: rolling 90-day expiry, throttled writes
- **On mint (login/setup/accept):** `created_at = now`, `last_seen_at = now`, `expires_at = now + session_ttl_days` (90, configurable).
- **On authenticated use (inside `require_auth`):** reject if `now >= expires_at` → 401. Otherwise **roll** the window — but **throttle the write:** only issue the `UPDATE sessions SET last_seen_at=now, expires_at=now+ttl WHERE token_hash=:h` when `now - last_seen_at >= session_bump_interval_seconds` (default 3600 = 1h). Within the interval, skip the write entirely. Net cost: at most one cheap PK UPDATE per hour per active session, never a write-per-request.
- Both `session_ttl_days` and `session_bump_interval_seconds` are Settings knobs.

### D5 — Auth dependency contract (`AuthContext`) — the contract ENG-65+ consume
- **`AuthContext`** (frozen dataclass in `msgd/auth/context.py`): fields `user_id: str`, `workspace_id: str`, `role: str`, `device_id: str`, `session_token_hash: str`, plus the loaded `user: User`, `device: Device`, `session: Session` ORM objects (bound to the request session). This is a read snapshot; routers needing live rows reuse the same `Depends(get_session)`.
- **`require_auth`** (FastAPI dependency, `msgd/api/deps.py`): reads `Authorization`, parses `Bearer <token>` (malformed/missing → 401 `/problems/unauthenticated`), computes `token_hash`, `SELECT` the session joined to user+device by `token_hash`, checks: session exists, `now < expires_at`, `user.deactivated_at IS NULL`. On any failure → **401** problem+json (uniform — never reveal which check failed). On success: perform the throttled rolling bump (D4), then return the `AuthContext`. Consumed as `ctx: Annotated[AuthContext, Depends(require_auth)]`.
- **`require_role(*roles)`** (dependency factory, `msgd/api/deps.py`): builds on `require_auth`; if `ctx.role not in roles` → **403** `/problems/forbidden`. Admin endpoints use `Depends(require_role("owner", "admin"))`.
- **Contract note for downstream tickets:** every protected M1 router takes `ctx: AuthContext`; workspace scoping, membership checks, and author-field validation (ENG-65 upload path) all read from `ctx`. Do not re-parse the header anywhere else.

### D6 — Rate limiter: in-process, reusable dependency
- **Mechanism:** a reusable **fixed-window counter** `RateLimiter(limit, window_seconds, *, now=monotonic)` in `msgd/auth/ratelimit.py`, keyed by an arbitrary string. In-memory `dict[key -> (window_start, count)]`. Injectable `now` (monotonic clock) so tests advance time without sleeping.
- **Auth application:** on `POST /v1/auth/login`, `POST /v1/setup`, and `POST /v1/auth/accept-invite`, check **two** buckets per §4.3 — **10/min per IP** and **10/min per email** — before running argon2 (cheap gate first, so a flood can't burn CPU). Either bucket exceeded → **429** `/problems/rate-limited` with a `Retry-After` header. The attempt still counts toward the window.
- **Reusability seam (ENG-66):** expose a dependency factory `rate_limit(limiter, key_fn)` so the events ticket reuses the same class with its own limits/keys (60/min/user, burst 20/s per §4.3). The auth limiter is one instance; ENG-66 constructs others.
- **Client IP:** `client_ip(request)` helper honoring a `trust_proxy` Settings flag (**default off** → `request.client.host`; when on → leftmost `X-Forwarded-For`). Documented: behind a reverse proxy the operator must set XFF and enable `trust_proxy`, else all callers share the proxy's IP bucket.
- **Concurrency honesty note (single worker):** the limiter is **per-process** in-memory state. The MVP runs **exactly one uvicorn worker** (§1, §11), so per-process == whole-server; correct as-is. Multi-worker/horizontal scaling would need a shared store (Redis) — explicitly out of MVP scope, documented in the module docstring. Read-modify-write happens synchronously on the event loop (no `await` between read and write), so no lock is needed; the CPU-bound argon2 verify runs in a threadpool (`asyncio.to_thread`), separate from limiter state.

### D7 — Admin surface: minimal (create + accept only)
- **`POST /v1/admin/invites {role, ttl_seconds}`** — `Depends(require_role("owner","admin"))`. Mint a 256-bit invite token (same discipline as D2), store `Invite(token_hash, workspace_id=ctx.workspace_id, created_by=ctx.user_id, role, expires_at=now+ttl)`. Return the **join URL once**: `{"url": "https://<host>/join/<raw_token>", "expires_at": ...}`. `role` restricted to `{member, guest, admin}` (an invite cannot mint an `owner`); validated in the request model.
- **`POST /v1/auth/accept-invite {token, email, display_name, password}`** — **unauthenticated** (the invite token *is* the authorization; rate-limited by IP+email per D6). Validate: hash the token, `SELECT` invite, check not expired and `used_by IS NULL`. **Single-use race guard:** atomically `UPDATE invites SET used_by=:new_user_id WHERE token_hash=:h AND used_by IS NULL RETURNING token_hash`; if no row returned → already consumed → **409/410** `/problems/invite-used`. Create the `User(role=invite.role, workspace_id=invite.workspace_id)`, then auto-login (mint device + session, return token — same response as login). Expired invite → **410** `/problems/invite-expired`.
- **`user.joined` emission → deferred to ENG-65.** Mark the seam at the acceptance call site (comment only, no stub).
- **Invite listing: NOT built in M1.** YAGNI — no admin UI consumes it yet; add when needed. Ruled minimal per ticket.

### D8 — Password policy + argon2 parameters
- **Policy:** minimum length **≥ 12**, **no composition rules** (NIST-aligned), maximum length **1024** (bounds argon2 work / avoids huge-input DoS). Enforced declaratively in the Pydantic request models (`Field(min_length=12, max_length=1024)`), configurable via `password_min_length` / `password_max_length` Settings.
- **argon2:** `argon2-cffi` — default algorithm is already **Argon2id**. **Pin the cost parameters explicitly in Settings** for auditability rather than inheriting library defaults that can drift across versions: `argon2_time_cost=3`, `argon2_memory_cost_kib=65536` (64 MiB), `argon2_parallelism=4`, `hash_len=32`, `salt_len=16`. Construct one module-level `PasswordHasher` from these in `msgd/auth/passwords.py`. Expose `needs_rehash()` for future parameter upgrades (call it on successful login; out-of-scope to act on now, but wire the check).

### Carryover (PR #12 security review) — config-gate `/docs`, `/redoc`, `/openapi.json`
- **Ruling:** add `docs_enabled: bool = False` to Settings (**secure prod default off**). In `create_app`, pass `docs_url`/`redoc_url`/`openapi_url = None` when disabled (FastAPI serves 404 for all three). Dev/compose sets `MSG_DOCS_ENABLED=true`; the test suite enables it for the schema/contract tests and has a dedicated gating test for both states.

### RFC 9457 problem+json convention (§3.2) — established here, inherited app-wide
- `msgd/api/problems.py`: a `Problem` pydantic model `{type: str, title: str, status: int, detail: str|None, instance: str|None}`, a `ProblemException(status, type, title, detail)` base, and named factory helpers (`unauthenticated`, `forbidden`, `rate_limited`, `already_initialized`, `invalid_device`, `invite_used`, `invite_expired`, `invalid_credentials`, ...). `type` is a **relative URI** `/problems/<slug>`.
- `register_problem_handlers(app)` (called in `create_app`) installs handlers for `ProblemException`, `RequestValidationError` (→ 422 problem+json), and `StarletteHTTPException` so **the entire app** emits `Content-Type: application/problem+json` with the standard fields. This is the convention every M1 endpoint inherits — no ad-hoc JSON errors anywhere.

---

## 3. File list

**New — `msgd/auth/` package (pure logic, no FastAPI coupling except deps.py):**
- `msgd/auth/__init__.py`
- `msgd/auth/passwords.py` — module-level `PasswordHasher` from Settings; `hash_password`, `verify_password` (threadpool-offloaded), precomputed `DUMMY_HASH` + dummy verify, `needs_rehash`.
- `msgd/auth/tokens.py` — `mint_token() -> (raw, token_hash)`, `hash_token(raw) -> str` (sha256 hex).
- `msgd/auth/context.py` — `AuthContext` frozen dataclass.
- `msgd/auth/sessions.py` — session create / lookup-by-hash / throttled rolling bump / revoke; device mint-or-reuse; used by routers + `require_auth`.
- `msgd/auth/ratelimit.py` — `RateLimiter` class, `client_ip(request)` helper, module docstring with the single-worker honesty note.

**New — API layer:**
- `msgd/api/problems.py` — `Problem` model, `ProblemException` + factories, `register_problem_handlers(app)`.
- `msgd/api/deps.py` — `require_auth` (→ `AuthContext`), `require_role(*roles)`, the auth rate-limit dependency wiring.
- `msgd/api/schemas/__init__.py`, `msgd/api/schemas/auth.py` — request/response models: `SetupRequest`, `LoginRequest`, `LoginResponse` (`token`, `user_id`, `device_id`, `workspace_id`, `role`, `expires_at`), `SessionInfo`, `SessionListResponse`, `CreateInviteRequest`, `InviteResponse`, `AcceptInviteRequest`.
- `msgd/api/routers/auth.py` — `POST /v1/setup`, `POST /v1/auth/login`, `GET /v1/auth/sessions`, `DELETE /v1/auth/sessions/{id}`, `POST /v1/auth/accept-invite`.
- `msgd/api/routers/admin.py` — `POST /v1/admin/invites`.

**Modified:**
- `msgd/api/app.py` — call `register_problem_handlers(app)`; pass `docs_url/redoc_url/openapi_url` gated by `settings.docs_enabled`; `include_router` for `auth` and `admin`.
- `msgd/settings.py` — add: `docs_enabled=False`, `trust_proxy=False`, `session_ttl_days=90`, `session_bump_interval_seconds=3600`, `password_min_length=12`, `password_max_length=1024`, `argon2_time_cost=3`, `argon2_memory_cost_kib=65536`, `argon2_parallelism=4`, `auth_rate_limit_per_minute=10`, `invite_default_ttl_seconds` / `invite_max_ttl_seconds`.
- `msgd/logging.py` — add the redaction `Filter` (denylist of sensitive `extra` keys) to the dictConfig handler.
- `msgd/db/models.py` — **(optional, recommended)** add `Index("ix_sessions_user_id", "user_id")` to `Session` for the sessions-list + bulk-revoke query. If added, it **must** ship with a paired migration (below) — the existing migration-parity test (`test_migrations.py`, `compare_metadata`) fails otherwise.
- `msgd/db/migrations/versions/0002_auth_indexes.py` — **only if** the model index is added; additive, non-breaking.
- `server/pyproject.toml` — add `argon2-cffi` to `dependencies`.

No new test-time deps (httpx / testcontainers / pytest-asyncio already in the root `dev` group).

---

## 4. Implementation steps (ordered)

1. **Deps + settings:** add `argon2-cffi` to `server/pyproject.toml`; add all Settings knobs (D8/D4/D6/docs). `uv lock`.
2. **Problem convention:** `msgd/api/problems.py` (model, exception, factories, `register_problem_handlers`). Wire into `create_app` first so all later endpoints error correctly.
3. **Docs gating:** thread `settings.docs_enabled` into `create_app`'s `FastAPI(...)` constructor.
4. **Auth primitives:** `passwords.py` (Settings-driven `PasswordHasher`, dummy hash, threadpool verify), `tokens.py` (mint/hash), `ratelimit.py` (`RateLimiter`, `client_ip`).
5. **Session/device logic:** `context.py` (`AuthContext`), `sessions.py` (device mint-or-reuse, session create, lookup-by-hash, throttled rolling bump, revoke).
6. **Dependencies:** `msgd/api/deps.py` — `require_auth`, `require_role`, auth rate-limit dep.
7. **Schemas:** `msgd/api/schemas/auth.py` with password `Field` constraints and invite-role restriction.
8. **Routers:** `routers/auth.py` (setup w/ advisory lock + zero-user guard; login w/ rate-limit → verify → device → session; sessions list/revoke; accept-invite w/ atomic single-use UPDATE), `routers/admin.py` (create invite). Mark the two ENG-65 emission seams.
9. **Wire-up:** `create_app` includes both routers; logging redaction filter.
10. **Models/migration (if index added):** update `models.py` + author `0002_auth_indexes.py`; confirm `test_migrations.py` parity.
11. **Tests** (§5).

---

## 5. Test plan (pytest against the real harness)

All request/round-trip tests use the container-backed `client` fixture (auto-marked `integration`). **Override Settings in tests to weak argon2 params** (`time_cost=1, memory_cost=8, parallelism=1`) — the 64 MiB production cost makes a suite full of logins unacceptably slow. Keep one test asserting the *production* defaults are the pinned values.

1. **`test_setup.py`** — first-run creates workspace + owner, returns a working token; second `POST /v1/setup` → 409 problem+json; owner role is `owner`. **Race test** (see Risks): two concurrent setups against a *committing* fixture ⇒ exactly one 200, one 409.
2. **`test_login.py`** — valid login → token + fields; wrong password → 401 generic; **unknown email → identical 401 body/status** (enumeration shape) + a spy asserting the dummy-verify branch ran; deactivated user → 401. **Rolling expiry:** age `last_seen_at` past the bump interval → authed request advances `expires_at`; a request *within* the interval leaves the row unchanged (no write). Expired session → 401.
3. **`test_sessions.py`** — multiple logins list via `GET /v1/auth/sessions` (current session flagged); `DELETE /v1/auth/sessions/{token_hash}` then an immediate request with that token → **401 (instant revocation)**; a user cannot revoke another user's session.
4. **`test_invites.py`** — owner/admin creates invite; **member → 403**; accept creates user + auto-login; **second accept same token → 409/410 (single-use)**; **expired invite → 410**; invite cannot request `owner` role (422).
5. **`test_rate_limit.py`** — 11th attempt in a window from one IP → 429 + `Retry-After`; same per-email; window advance (injected clock) resets. No `sleep`.
6. **`test_auth_errors.py`** — missing / malformed `Authorization` → 401 problem+json; validation error → 422 problem+json; assert `Content-Type: application/problem+json` and the standard fields across representative endpoints.
7. **`test_docs_gating.py`** — `docs_enabled=False` ⇒ `/docs`, `/redoc`, `/openapi.json` all 404; `True` ⇒ 200.
8. **`test_no_secrets_in_logs.py`** *(security-sensitive AC)* — drive setup + login + accept-invite while capturing all log output (attach a handler / `caplog`); assert the **raw token never appears** in any record (message or `extra`), and passwords never appear.
9. **`test_auth_context.py`** — mount a throwaway protected probe route in-test; assert `require_auth` yields correct `user_id/workspace_id/role/device_id`, and `require_role` rejects a wrong role with 403.

Also: `ruff` + `mypy --strict` clean (both gate the repo); the migration-parity test stays green.

---

## 6. Risks / open questions

- **Harness can't do real concurrency.** The `client` fixture shares one connection/session (rollback isolation), so the advisory-lock setup race and any true parallel-login test can't run through it. **Mitigation:** add a small **committing** fixture (own `create_async_engine`, real transactions, manual cleanup / throwaway DB) used *only* by the race test. Flag to reviewer; keep the isolated fixture as the default for everything else.
- **argon2 cost in tests.** Must override to weak params or the suite crawls (each login hashes). Handled via Settings override; one test pins production defaults.
- **`{id}` for session revoke = `token_hash`.** §4.2 has no separate `session_id` column and the schema is frozen. Ruling: the list/revoke id **is** the `token_hash` (hex) — it is not a credential (cannot be reversed to the bearer token), and a user can only enumerate/revoke their own sessions. Avoids a schema change. Called out for review; alternative (add `session_id` column) is a heavier §4.2 divergence and rejected for M1.
- **Reverse-proxy IP.** With `trust_proxy` off (default), all traffic behind a proxy shares one IP bucket, weakening per-IP limiting. Documented; operator enables `trust_proxy` + XFF. Acceptable for MVP.
- **Two ENG-65 seams** (`workspace.created`+`user.joined` on setup; `user.joined` on accept) are comment-marked, not stubbed — a workspace/user can exist in M1 without its meta-stream events. Confirm ENG-65 backfills or emits on first stream bootstrap.
- **Single-workspace assumption** (login/setup gate on global `count(users)`, email effectively global). Matches D6/§7; revisit only if multi-workspace-per-server is ever in scope (email+workspace lookup).
- **`needs_rehash` wired but not acted on** — no argon2 param-upgrade flow in M1 (would need to re-hash on login with the plaintext in hand). Note it; defer the action.

---

## 7. Agent assignment

All work is `server/` → **`python-engineer`** for implementation and tests. No `ui-engineer` / `devops-engineer` involvement (compose already documents the one-worker constraint; docs gating is a Settings default, not a compose change — though the dev compose/`.env` should later set `MSG_DOCS_ENABLED=true`, that's a one-line note for whoever owns the compose file, not blocking this ticket).
