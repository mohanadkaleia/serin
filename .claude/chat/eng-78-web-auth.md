# ENG-78 — M2: Web auth (login / invite-accept, session in worker + meta, device_id persistence)

Tech-lead plan. Implementer: **ui-engineer**, entirely in `web/`. Do not implement from this file
until the plan is accepted.

## Goal (restated)

Give the M2 web client a real authenticated entry: Login / Setup / Accept-invite views, a Vue Router
auth gate, and — the load-bearing part — an auth layer **inside the SharedWorker** that owns the
session token, drives the server auth endpoints, persists the session + `device_id` in the Dexie
`meta` table for reload survival, and exposes the token only worker-internally (to the authed HTTP
client and, later, the ENG-79 WS connect). Tabs never see the raw token.

This ticket does **not** build the sync engine (ENG-79), projections (ENG-80), mutations (ENG-81),
or the real app shell (ENG-82). The post-login landing is a placeholder authed view.

## Source-of-truth references (read before coding)

- **Server auth API** — `server/msgd/api/routers/auth.py` + `server/msgd/api/schemas/auth.py`.
  Exact shapes (all responses are `LoginResponse` unless noted):
  - `POST /v1/setup` — req `{workspace_name, email, password, display_name}` → `LoginResponse`.
    409 `already-initialized` once a workspace exists.
  - `POST /v1/auth/login` — req `{email, password, device_label, device_id?}` → `LoginResponse`.
    Note **`device_label` is required** (min 1, max 200); `device_id` is optional (omit on first login).
  - `POST /v1/auth/accept-invite` — req `{token, email, display_name, password}` → `LoginResponse`.
    **No `device_label` / `device_id` fields** — the server mints a fresh device (label `None`).
  - `GET /v1/auth/sessions` / `DELETE /v1/auth/sessions/{id}` — authed; sessions UI is a later ticket,
    but the authed-fetch client must support them (bearer + 204/list parsing). Wire the client; the
    settings UI is out of scope here.
  - `LoginResponse = {token, user_id, device_id, workspace_id, role, expires_at}` — the raw token is
    returned **exactly once**, here.
  - Password policy: `min_length=12`, `max_length=1024`. Mirror the 12-char minimum client-side for UX.
- **problem+json** — `server/msgd/api/problems.py`. Every error is `application/problem+json` with
  `{type, title, status, detail, instance}`, `type` = `/problems/<slug>`. Slugs the client must map:
  `invalid-credentials` (401), `unauthenticated` (401), `rate-limited` (429, `Retry-After` header),
  `already-initialized` (409), `invalid-device` (400), `invite-used` (410), `invite-expired` (410),
  `invalid-invite` (404), `account-conflict` (409), `validation-error` (422).
- **TDD §7 (D6)** — device_id per browser install, persisted in IndexedDB `meta`, reused on re-login;
  session token stored in worker-held memory + IndexedDB; rolling 90-day expiry.
- **TDD §5.1 / §5.2** — SharedWorker owns WS + IndexedDB + sync + outbox; `meta` table is
  `"key"`-indexed KV (holds `projection_version`, session info, `my_user_id`).
- **TDD §3.3 + `server/msgd/ws/router.py`** — WS auth is **`Sec-WebSocket-Protocol: bearer, <token>`**,
  NOT `?token=`. Browser form: `new WebSocket(url, ["bearer", token])`; server echoes
  `accept(subprotocol="bearer")`. ENG-79 uses this; ENG-78 only exposes the token to the worker's
  connect path.
- **Merged worker seam** — `web/src/worker/{types,core,client,db,rpc}.ts`. Extension points:
  `RpcRequest` union (`types.ts`), `RpcResultMap` + `WorkerCore.register()` (`core.ts`), the
  `WorkerClient` surface (`client.ts`), `MsgDb.metaGet/metaPut` (already present).

---

## Architecture decisions (rulings)

### R1 — Token-storage boundary: worker-owned, never to tabs (KEY security boundary)

The session token lives in exactly two places, **both worker-side**:

1. **In-memory** in the SharedWorker: an `AuthManager` (new `worker/auth.ts`) owned by `WorkerCore`
   holds `session: SessionState | null` — the single owner of the token, the WS, and the HTTP client.
2. **IndexedDB `meta`** (for reload persistence), as discrete KV rows:
   - `session_token` — the raw bearer token.
   - `device_id` — per-browser-install device identity (see R3; survives logout).
   - `my_user_id`, `workspace_id`, `role`, `session_expires_at` — cached identity.
   - `server_url` — **optional**; omitted by default. The web app is served same-origin by FastAPI
     (§5.1), so the HTTP client uses **relative `/v1/...` paths** and the WS uses a same-origin URL.
     Reserve the key for a future multi-server client; do not populate it in M2.

**The boundary rule (spec precisely):**
- The raw token is used **only** worker-side: as `Authorization: Bearer <token>` on HTTP requests and
  as the `["bearer", token]` WS subprotocol. Both are attached inside the worker.
- The token is **never** returned to a tab over RPC. `auth.status` (and every other tab-facing result)
  returns identity only: `{authenticated, my_user_id, workspace_id, role, expires_at}` — **no token**.
- The token is **never** rendered to the DOM, logged, or `console.*`-ed. No `auth.*` handler or HTTP
  wrapper may include the token in an error detail, an RPC result, a thrown message, or a log line.
- A tab that needs an authed server call asks the worker to perform it (today via the `auth.*` verbs;
  later via sync/mutation verbs the worker already owns); the worker attaches the token. Tabs issue
  intent, not credentials.
- Security posture: a bearer token in IndexedDB is the standard SPA session tradeoff (equivalent to a
  token in `localStorage`/an httpOnly-less store) — accepted here. Confining it to the worker realm and
  off the DOM removes the casual-XSS-reads-the-input surface and keeps it out of any tab render tree.
  The `assertCloneable` dev-guard in `rpc.ts` already fires on non-clonable RPC payloads; our defense is
  a **unit test asserting the token never appears in any `FromWorker` frame** (R7).

### R2 — Authed HTTP client (`worker/http.ts`)

A small injectable fetch wrapper — the base every ENG-79/81 server call reuses. Transport-agnostic and
mockable (mirrors the ENG-77 core seam: no real network needed to test auth logic).

```ts
export interface ApiError {
  status: number
  code: string            // problem `type` slug, e.g. 'invalid-credentials' (from /problems/<slug>)
  title: string
  detail?: string
  retryAfter?: number      // parsed from Retry-After on 429
}
export type ApiResult<T> = { ok: true; value: T } | { ok: false; error: ApiError }

export interface HttpClient {
  post<T>(path: string, body: unknown, opts?: { authed?: boolean }): Promise<ApiResult<T>>
  get<T>(path: string): Promise<ApiResult<T>>            // authed
  del(path: string): Promise<ApiResult<void>>            // authed (204)
}

export interface HttpClientDeps {
  baseUrl?: string                       // default '' → relative /v1 paths (same-origin, §5.1)
  fetchImpl?: typeof fetch               // injected in tests
  getToken: () => string | null          // worker-held token accessor
  onUnauthorized: () => void | Promise<void>  // 401 → clear session (see R6-ish)
}
export function createHttpClient(deps: HttpClientDeps): HttpClient
```

Behavior:
- Attaches `Authorization: Bearer <token>` when `authed` (default true; login/setup/accept-invite pass
  `authed:false`) and `getToken()` is non-null. `Content-Type: application/json` on bodies.
- Parses `application/problem+json` into `ApiError` (`code` = last path segment of `type`). On a non-JSON
  / opaque failure, synthesize `ApiError{status, code:'http-<status>', title:'Request failed'}`.
- On **401**: call `onUnauthorized()` (session invalid → clear token + meta, surface re-login) **before**
  returning the typed error. This is the single choke point that turns an expired/revoked session into a
  re-login state for the whole app.
- Never throws for HTTP errors — always returns `ApiResult`. Only a network/`fetch` rejection surfaces as
  `ApiError{status:0, code:'network'}`.

### R3 — device_id flow (server mints, client persists, reuses)

One device per browser install (§7). `device_id` lives in `meta['device_id']`.
- **First login / setup / accept-invite** (no stored device_id): send the login request with
  `device_id` omitted (login) or the endpoint mints unconditionally (setup/accept-invite). Take
  `response.device_id` and persist it to `meta`.
- **Re-login** (stored device_id present): include it as `LoginRequest.device_id`; the server's
  `mint_or_reuse_device` reuses it.
- **Self-heal on `invalid-device` (400):** if a login with a stored `device_id` returns
  `invalid-device` (e.g. the device belongs to a different user on a shared machine, or was pruned),
  clear `meta['device_id']` and retry the login **once** without it → server mints fresh → persist.
  This keeps re-login correct without leaking device identity between users.
- **`device_label`** (login only, required): the worker computes a stable, human-readable label from
  the environment (e.g. a coarse `navigator.userAgent`-derived "Chrome on macOS", bounded ≤200 chars,
  with a safe fallback like "Web browser"). Setup/accept-invite send no label (server uses `None`).
- **device_id survives logout** (see R6): it is browser-install identity, not session state.

### R4 — Login-via-worker-RPC (ruling)

**The tab calls a worker RPC `auth.login(credentials)`; the worker performs the POST, stores the token,
and returns success WITHOUT the token.** Credentials transit tab→worker over the in-process
`postMessage` RPC (structured-clone, never a network hop from the tab) and the raw token is created,
stored, and used entirely worker-side — it never touches tab JS. This is strictly cleaner than "tab
POSTs directly and hands the worker a token," which would (a) put the raw token in tab memory/render
tree and (b) split token ownership across realms. Setup and accept-invite follow the same shape.

Trade note: yes, plaintext credentials cross the tab→worker boundary. That is unavoidable (the user
types them into a tab DOM input) and acceptable — the *durable* secret (the long-lived bearer token) is
what we keep worker-only. The worker holds credentials only transiently to POST them, then discards.

### R5 — `auth.*` RPC verbs (extend the ENG-77 taxonomy)

Add five verbs to the taxonomy. `WorkerCore` gains **real handlers** (delegating to `AuthManager`), not
stubs, registered via the existing per-method `register()` seam.

`types.ts` — extend the `RpcRequest` union and add DTOs (all structured-clone-safe plain data):
```ts
export interface LoginCredentials { email: string; password: string }
export interface SetupCredentials {
  workspace_name: string; email: string; password: string; display_name: string
}
export interface AcceptInviteCredentials {
  token: string; email: string; display_name: string; password: string
}
export interface AuthStatus {                 // TOKEN-FREE by construction
  authenticated: boolean
  my_user_id?: string
  workspace_id?: string
  role?: string
  expires_at?: string
}
// RpcRequest union additions:
//   | { method: 'auth.login';        params: LoginCredentials }
//   | { method: 'auth.setup';        params: SetupCredentials }
//   | { method: 'auth.acceptInvite'; params: AcceptInviteCredentials }
//   | { method: 'auth.logout';       params: Record<string, never> }
//   | { method: 'auth.status';       params: Record<string, never> }
```
`core.ts` — extend `RpcResultMap`: `auth.login | auth.setup | auth.acceptInvite | auth.status` →
`AuthResult` (`{ ok: true; status: AuthStatus } | { ok: false; error: ApiError }`, token-free);
`auth.logout` → `{ ok: true }`. Register handlers in a new `registerAuth()` step that delegates to the
`AuthManager` instance.

`client.ts` / `index.ts` — extend `WorkerClient` with an `auth` namespace so stores stay off the wire:
```ts
auth: {
  login(c: LoginCredentials): Promise<AuthResult>
  setup(c: SetupCredentials): Promise<AuthResult>
  acceptInvite(c: AcceptInviteCredentials): Promise<AuthResult>
  logout(): Promise<{ ok: true }>
  status(): Promise<AuthStatus>
}
```
(Implemented over the same `caller.request({ method:'auth.login', params })` plumbing.) Note the auth
verbs return **application-level results** (`{ ok:false, error }`) rather than rejecting the RPC — a
wrong password is not a transport failure. Keep the `RpcCallError` reject path for genuine
transport/handler faults only.

### R6 — `AuthManager` (`worker/auth.ts`) — the session owner

Owned by `WorkerCore` (constructed in the ctor/`init`), given `(db: MsgDb, http: HttpClient)` where the
HTTP client's `getToken` reads this manager's in-memory token and `onUnauthorized` calls
`this.clearSession()`. Responsibilities:
- `restore()` (run in `WorkerCore.init()` after `checkProjectionVersion`): hydrate the in-memory session
  from `meta` (`session_token`, ids, expiry). Enables reload persistence.
- `login/setup/acceptInvite`: build the request (R3 device rules), POST via `http` (`authed:false`),
  on success persist `{session_token, device_id, my_user_id, workspace_id, role, session_expires_at}` to
  `meta` and set the in-memory session; return a token-free `AuthResult`.
- `status()`: return `AuthStatus` from the in-memory session (token-free).
- `logout()`: clear the in-memory token + the session `meta` rows (`session_token`, `my_user_id`,
  `workspace_id`, `role`, `session_expires_at`) — **always**. **Keep `device_id`** (browser-install
  identity, reused on next login). Also `db.clearDerivedTables()` — lean clear so a shared machine does
  not leak cached messages/streams to the next user (events/outbox are left for ENG-79/80 to manage;
  clearing derived tables is the cheap, correct-by-construction wipe available now). Best-effort call the
  server `DELETE`/logout is out of scope (no bulk-logout endpoint; a future ticket can revoke the current
  session via `DELETE /v1/auth/sessions/{id}` using the session id). Document that.
- `getToken(): string | null` — **worker-internal** accessor for the HTTP client and (R8) the WS connect.
  Not reachable from any tab.

### R7 — Logout

Covered in R6: clear in-memory + session `meta` always; keep `device_id`; `clearDerivedTables()`; the
auth store then routes the tab back to `/login`. `auth.status` subsequently returns
`{authenticated:false}`, so every tab's gate re-locks on its next `status()` (or on a broadcast, if we
add one — not required for M2; a `location.reload()` after logout is acceptable and simplest).

### R8 — WS subprotocol exposure for ENG-79

ENG-78 does not open a socket. It exposes `AuthManager.getToken()` (worker-internal) and documents, in
`auth.ts`, the exact ENG-79 connect contract:
```ts
// ENG-79 (sync engine, worker-side ONLY): const token = auth.getToken()
//   new WebSocket(wsUrl, ['bearer', token])   // Sec-WebSocket-Protocol: bearer, <token> (TDD §3.3)
// NOT ?token= — the raw token must never appear in a URL.
```
No token ever crosses to a tab for this; the WS is worker-owned.

### R9 — Views, router, auth store

- **`stores/auth.ts`** (Pinia): the tab-side auth state, fed by the worker client's `auth` namespace.
  State: `phase: 'unknown' | 'anonymous' | 'authenticated'`, `myUserId`, `workspaceId`, `role`. Actions:
  `init()` (calls `client.auth.status()` once the worker is `ready()`), `login/setup/acceptInvite`
  (call the worker, map `AuthResult.error` → a display message via a small `problem→message` map,
  update `phase` on success), `logout()`. **Holds no token** — only identity.
- **`views/LoginView.vue`** — email + password form → `authStore.login`. Client-side: email shape,
  password ≥12. Renders typed errors (invalid-credentials → "Incorrect email or password";
  rate-limited → "Too many attempts, try again in Ns"). On success → redirect to the post-login landing
  (or the `redirect` query param).
- **`views/SetupView.vue`** — first-run: workspace_name + email + password + display_name → `auth.setup`.
  On `already-initialized` (409) show "This workspace is already set up" with a link to `/login`.
- **`views/AcceptInviteView.vue`** — route `/join/:token`; reads the invite token from the path param,
  collects email + display_name + password → `auth.acceptInvite`. Handles `invite-used`/`invite-expired`
  (410) and `invalid-invite` (404) with clear copy; `account-conflict` (409) → "An account for this email
  already exists."
- **Post-login landing** — a minimal placeholder authed view (the real shell is ENG-82). Reuse
  `HomeView.vue` as a stand-in "You're signed in as {display}" screen with a Logout button, or add a
  thin `views/AppShellView.vue` placeholder. Keep it tiny; ENG-82 replaces it.
- **`router/index.ts`** — add routes `/login`, `/setup`, `/join/:token`, and the authed landing (`/`).
  A global `beforeEach` guard: if `authStore.phase === 'unknown'` await `init()`; then unauthenticated
  access to a protected route → redirect `/login?redirect=<path>`; authenticated access to `/login`
  or `/setup` → redirect `/`. `/login`, `/setup`, `/join/:token` are the public routes. First-run
  routing (whether to show setup) is not auto-detected in M2 — `/setup` is directly reachable and
  self-reports `already-initialized`; no probe endpoint is added.
- **`App.vue` / bootstrap** — create the single `WorkerClient`, `await ready()`, kick `authStore.init()`
  before/at first route resolution so the guard has a real phase. Wire the client into the store (e.g.
  provide/inject or a module singleton — follow the ENG-82 seam intent; a module-level `getWorkerClient()`
  singleton is fine for M2).

---

## File list

**New (worker):**
- `web/src/worker/http.ts` — `createHttpClient`, `ApiResult`/`ApiError`, problem+json parsing, bearer
  attach, 401→`onUnauthorized`. Injectable `fetchImpl`.
- `web/src/worker/auth.ts` — `AuthManager` (in-memory session + meta persistence, login/setup/
  acceptInvite/logout/status, `restore()`, `getToken()`, device_id + device_label logic, meta-key consts).

**Edit (worker):**
- `web/src/worker/types.ts` — `RpcRequest` union additions; `LoginCredentials`/`SetupCredentials`/
  `AcceptInviteCredentials`/`AuthStatus`/`AuthResult` DTOs; session meta-key constants.
- `web/src/worker/core.ts` — `RpcResultMap` additions; construct `AuthManager` (+ default `HttpClient`);
  call `auth.restore()` in `init()`; `registerAuth()` handlers; expose `getToken()` for ENG-79.
- `web/src/worker/client.ts` — `WorkerClient.auth` namespace over `caller.request`.
- `web/src/worker/index.ts` — export the new auth types (`AuthStatus`, credential DTOs, `AuthResult`).

**New (tab):**
- `web/src/stores/auth.ts` — Pinia auth store (identity only, no token).
- `web/src/views/LoginView.vue`, `web/src/views/SetupView.vue`, `web/src/views/AcceptInviteView.vue`.
- (optional) `web/src/views/AppShellView.vue` placeholder landing, else reuse `HomeView.vue`.

**Edit (tab):**
- `web/src/router/index.ts` — routes + auth-gate `beforeEach`.
- `web/src/App.vue` / `web/src/main.ts` — worker-client bootstrap + `authStore.init()` wiring.

**Tests:**
- `web/tests/unit/worker/http.spec.ts` — bearer attach, problem+json parse, 401→onUnauthorized,
  non-JSON/opaque failure, `Retry-After` parse, network reject.
- `web/tests/unit/worker/auth.spec.ts` — the core assertions (below), against MemoryDb + fake-idb, with
  an injected fake fetch.
- `web/tests/unit/views/LoginView.spec.ts` — Vue Test Utils component test (form → store action, error
  render, disabled-while-submitting).
- (extend `web/tests/unit/worker/core.spec.ts` if convenient for the `auth.*` round-trip through
  `WorkerCore` with an injected fake HTTP client.)

---

## Step-by-step

1. `types.ts`: add the credential/status DTOs, `RpcRequest` `auth.*` members, session meta-key consts.
2. `http.ts`: implement `createHttpClient` + problem+json parsing; unit-test with a fake fetch first.
3. `auth.ts`: implement `AuthManager` against the `HttpClient` + `MsgDb`; device_id/label rules;
   `restore()`, `getToken()`, logout wipe. Unit-test with fake fetch + MemoryDb/fake-idb.
4. `core.ts`: construct the manager (+ default HTTP client with `getToken`/`onUnauthorized` wired), call
   `restore()` in `init()`, register `auth.*` handlers, expose `getToken()`. Add `RpcResultMap` entries.
   Make the HTTP client injectable via an optional `WorkerCore` dep so tests pass a fake (production
   default = real `fetch`, same-origin relative paths).
5. `client.ts` + `index.ts`: add the `auth` namespace + exports.
6. `stores/auth.ts`: Pinia store over the client; `problem→message` map.
7. Views: Login, Setup, AcceptInvite (+ placeholder landing). Tailwind, minimal but clean.
8. `router/index.ts` + `App.vue`/`main.ts`: routes, guard, bootstrap.
9. Tests: http, auth, LoginView component. Run `pnpm test`, `pnpm lint`, `pnpm type-check`.

## Test plan (assertions)

`auth.spec.ts` (injected fake fetch returning canned `LoginResponse` / problem+json):
- **login stores token + device_id in meta** — after `auth.login`, `meta['session_token']`,
  `meta['device_id']`, `meta['my_user_id']`, `meta['workspace_id']` are set (fake-idb + MemoryDb).
- **token never leaves the worker** — no `FromWorker` frame produced by any `auth.*` handler contains the
  token string; `auth.status`/`auth.login` results are token-free. (Assert by scanning the serialized
  result/frames for the known token value.)
- **401 clears session** — a subsequent authed call returning 401 triggers `onUnauthorized` → in-memory
  token cleared + session meta rows removed; `auth.status` → `{authenticated:false}`.
- **re-login reuses device_id** — first login (no device_id) persists `d_…`; second login sends that
  `device_id` in the request body (assert the captured request payload); `device_id` unchanged.
- **invalid-device self-heal** — a login with a stored device_id returning `invalid-device` retries once
  without it, mints fresh, persists the new id.
- **problem+json parsed** — `invalid-credentials` → `AuthResult{ok:false, error:{code:'invalid-credentials',
  status:401}}`; `rate-limited` surfaces `retryAfter`.
- **logout** — clears session meta + in-memory token, **keeps `device_id`**, calls `clearDerivedTables`.
- **restore** — pre-seed session meta, construct a fresh manager, `restore()` → `auth.status` reports
  authenticated (reload persistence).

`http.spec.ts` — bearer header present iff authed + token; problem+json → typed `ApiError`; 401 path;
non-JSON body; network reject → `status:0`.

`LoginView.spec.ts` (Vue Test Utils, stubbed store) — submit calls `login` with the typed fields; an
error result renders the mapped message; the button disables while submitting.

## Risks / open questions

- **R-a (device_label source):** `navigator.userAgent` parsing is fuzzy; keep the label coarse and always
  provide a non-empty fallback so `LoginRequest.device_label` (min 1) never validation-fails. Not
  security-relevant — cosmetic in the sessions list.
- **R-b (token-leak regressions):** the whole ticket hinges on R1. The "token never in any frame" test is
  the guardrail; keep it and extend it if new tab-facing results are added. Reviewers should grep that no
  `auth.*` result type carries `token`.
- **R-c (RPC timeout vs argon2):** login involves argon2id server-side; the default 15s RPC timeout
  (`rpc.ts`) is generous but confirm the auth POST isn't gated behind a slower path. If needed, the auth
  verbs can pass a longer per-call timeout — prefer not to unless a test shows a real limit.
- **R-d (cross-tab logout coherence):** M2 accepts eventual re-lock (next `status()` / reload). A worker→tab
  `auth` status push is a clean follow-up but not required here; note it for ENG-82.
- **R-e (first-run detection):** no probe endpoint; `/setup` self-reports `already-initialized`. If UX wants
  auto-redirect to setup on a virgin server, that needs a lightweight server signal — out of scope, flag
  for product.
- **R-f (HTTP client injection reach):** `WorkerCore` is constructed in three places (solo/leader/
  shared-worker); the injectable-HTTP dep must default cleanly to real `fetch` so only tests inject. Keep
  the default path zero-config.
- **R-g (accept-invite device):** accept-invite mints a device server-side with no label and returns a
  `device_id`; persist it exactly like login so the next (login) re-uses it. Verify the returned
  `device_id` is stored on the accept-invite path too.

---

## Summary (for the caller)

- **Token-storage boundary:** the SharedWorker owns the token — in-memory in a new `AuthManager` plus
  IndexedDB `meta` rows (`session_token`, `device_id`, `my_user_id`, `workspace_id`, `role`,
  `session_expires_at`, optional `server_url`). Tabs **never** receive the raw token; it is used only
  worker-side for `Authorization: Bearer` and the WS `["bearer", token]` subprotocol, and is never
  rendered/logged/returned over RPC. Guardrail: a unit test asserting the token appears in no `FromWorker`
  frame.
- **Authed-fetch HTTP client (`worker/http.ts`):** `createHttpClient({baseUrl?, fetchImpl?, getToken,
  onUnauthorized})` → `post/get/del` returning `ApiResult<T>`; attaches the bearer, parses problem+json
  into typed `ApiError` (code = `/problems/<slug>` tail, `retryAfter` on 429), routes 401 → clear session.
  Injectable fetch = testable without a server; the base for every ENG-79/81 call.
- **device_id:** server mints on first login (omit `device_id`) / setup / accept-invite; client persists
  in `meta`; re-login sends the stored id (server reuses); `invalid-device` → drop + retry once fresh;
  `device_id` **survives logout** (browser-install identity). `device_label` (login-only, required) is a
  coarse UA-derived string with a fallback.
- **`auth.*` RPC verbs:** `auth.login`, `auth.setup`, `auth.acceptInvite`, `auth.logout`, `auth.status`
  added to the `RpcRequest`/`RpcResultMap` taxonomy and the `WorkerClient.auth` namespace; `WorkerCore`
  gets **real** handlers delegating to `AuthManager` via the existing `register()` seam. Auth results are
  application-level `{ok, status|error}` (token-free), not RPC rejections.
- **Login-via-worker-RPC ruling:** the tab calls `client.auth.login(credentials)`; the worker POSTs
  `/v1/auth/login`, stores the token, and returns success **without** it. Credentials cross tab→worker
  over in-process postMessage (never a tab network hop); the durable token stays worker-only.
- **WS-subprotocol exposure for ENG-79:** `AuthManager.getToken()` is worker-internal; `auth.ts` documents
  `new WebSocket(url, ['bearer', token])` (TDD §3.3, NOT `?token=`). No socket is opened in ENG-78.
- **Logout:** clear in-memory token + session `meta` always, keep `device_id`, `clearDerivedTables()`
  (lean wipe for shared machines), route to `/login`.
- **Files:** new `worker/{http,auth}.ts`, `views/{LoginView,SetupView,AcceptInviteView}.vue`,
  `stores/auth.ts`, tests `worker/{http,auth}.spec.ts` + `views/LoginView.spec.ts`; edits to
  `worker/{types,core,client,index}.ts`, `router/index.ts`, `App.vue`/`main.ts`. All `web/`, ui-engineer.
- **Top risks:** token-leak regressions (mitigated by the no-token-in-frame test — the KEY guardrail),
  device_label UA fuzziness (coarse + fallback), and keeping the injectable HTTP dep defaulting cleanly to
  real `fetch` across all three transports.
