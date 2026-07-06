# ENG-77 — M2: SharedWorker foundation (Dexie schema, tab↔worker RPC, Web Locks leader-election fallback)

**Milestone:** M2 — Web client + sync proof.
**Tech-lead:** planning complete; implementation delegated to **ui-engineer** (all parts).
**TDD refs:** §5.1 (SharedWorker owns all shared mutable state; tabs are dumb views; Web Locks fallback for Safari < 16, D4), §5.2 (the exact Dexie schema — copied verbatim below), §2.3 / §5.2 (`PROJECTION_VERSION` guards derived tables; mismatch ⇒ rebuild), assessment §3.14 (multi-tab: one SharedWorker or Web-Locks leader owns socket + cache; "cheap to do early, miserable to retrofit").
**Builds on:** ENG-75 web scaffold — `vite.config.ts` already has `worker: { format: 'es' }` and dev proxy; `tsconfig.app.json` already includes the `WebWorker` lib; `web/src/worker/README.md` is the documented seam this ticket fills.

## Goal (restated)

Build the **worker shell**: the process/leader that owns the Dexie database and exposes **one RPC surface** that every Pinia store (ENG-82) and every later engine (ENG-79 sync, ENG-80 projections, ENG-81 outbox) plugs into. This ticket ships the *container and contract*, not the behaviour:

1. A **`WorkerClient`** interface — the single object stores call — backed by **either** a real `SharedWorker` **or** an in-page **Web Locks leader** (BroadcastChannel), chosen at runtime by feature detection. Same RPC surface both ways.
2. The **Dexie schema per §5.2, verbatim**, with `PROJECTION_VERSION` in `meta` guarding derived tables and the drop-derived-tables/rebuild *plumbing* (the real rebuild is ENG-80; here a stub).
3. A **typed tab↔worker RPC**: discriminated-union request/response with correlation IDs + a worker→tab **subscription/push** channel, structured-clone-safe, with an **extensible method/topic taxonomy** ENG-79/81 add to without touching the transport.
4. **Graceful degradation**: IndexedDB absent/failing (private browsing) ⇒ Map-backed in-memory store, still works online (no persistence).
5. A **bounded-cache eviction stub** (newest ~2,000 events/stream; never touches `outbox`).
6. The **transport-agnostic-core testability seam** so the sync engine and projections are unit-testable in vitest with `fake-indexeddb`, no browser.

**NOT in scope** (these fill seams this ticket declares): the WebSocket / sync engine (ENG-79), client projections (ENG-80), the outbox drain loop (ENG-81), the Pinia stores that consume `WorkerClient` (ENG-82), Playwright multi-tab golden path (ENG-83). Query/mutate handlers ship here as **stubs** with a registry the later tickets extend.

**Areas touched:** new files under `web/src/worker/`, new suites under `web/tests/unit/worker/`, two new entries in `web/package.json` (`dexie` dep, `fake-indexeddb` dev). No server, cli, or CI-config changes (the existing `web` CI job already runs `pnpm test`/`typecheck`/`lint`).

---

## Decisions pinned

### D-1 · The core architecture — ONE `WorkerClient`, dual transport, chosen by feature detection

A `SharedWorker` is a single script instance shared by all same-origin tabs; it is the natural owner of the socket + cache. **Safari < 16 has no `SharedWorker`.** The fallback is a **leader tab** elected via the Web Locks API (`navigator.locks`) that runs the *same* worker logic in-page; other tabs are followers that RPC to the leader over `BroadcastChannel`.

**Rule the abstraction:** stores never learn which transport is live. They receive **one `WorkerClient`** whose surface is identical across:

- **(a) SharedWorker transport** — `new SharedWorker(new URL('./shared-worker.ts', import.meta.url), { type: 'module' })`; RPC over the connection `port` (`postMessage`).
- **(b) Leader transport** — `BroadcastChannel('msg-worker')` for messaging + `navigator.locks.request('msg-worker-leader', …)` for election. The winning tab hosts a `WorkerCore` in-page; followers proxy to it over the channel.

Selection happens once, at `createWorkerClient()`, by feature detection: `typeof SharedWorker !== 'undefined'` → (a), else (b). If neither `SharedWorker` **nor** `navigator.locks` exists (very old browsers), fall back to a **degenerate single-tab leader** (`WorkerCore` in the tab, no election, no channel) so the app still runs — multi-tab coherence is simply not guaranteed there, which is acceptable (documented).

This is the contract ENG-78/79/80/81/82 build on, so the interface is specified precisely:

```ts
// worker/types.ts — the contract every store depends on
export interface WorkerClient {
  /** Resolves once the worker/leader is reachable and the DB is open (persistent or degraded). */
  ready(): Promise<void>

  /** Read a projection. Discriminated on params.q; result type keyed to the query. */
  query<Q extends QueryParams>(params: Q): Promise<QueryResult<Q>>

  /** Enqueue a durable mutation (outbox write), set read-state, etc. Discriminated on params.m. */
  mutate<M extends MutateParams>(params: M): Promise<MutateResult<M>>

  /** Subscribe to worker→tab pushes (projection updates, status). Returns an unsubscribe fn. */
  subscribe<T extends Topic>(topic: T, handler: (payload: PushPayload<T>) => void): Unsubscribe

  /** Current transport/connection status, and a subscribe for changes. */
  status(): WorkerStatus
  onStatus(handler: (s: WorkerStatus) => void): Unsubscribe

  /** Detach this tab (close port / leave channel). Idempotent. */
  dispose(): void
}

export type Unsubscribe = () => void
export type WorkerStatus =
  | { transport: 'shared-worker' | 'leader' | 'solo'; db: 'persistent' | 'memory'; role: 'leader' | 'follower' | 'n/a' }
```

`query`/`mutate`/`subscribe` are **generic over the request discriminant** so adding a query (ENG-80) or a mutate (ENG-81) is a type-level extension of the `QueryParams`/`MutateParams` unions plus a handler registration in `WorkerCore` — **no change to `WorkerClient`, the transports, or the stores' call sites**.

### D-2 · Hand-rolled RPC, not comlink

**Rule: hand-rolled.** Weighed comlink (ergonomic `Comlink.wrap<T>()` proxy over a `MessagePort`) against a ~150-line typed protocol:

- **Dual transport.** Comlink is built around `MessagePort` endpoints. Our leader path is `BroadcastChannel` + Web Locks with a *target-tab addressing* need (many followers, one leader); we'd write a custom comlink `Endpoint` adapter for it anyway, erasing the ergonomic win.
- **Server-push subscriptions.** The worker→tab projection-update channel is not request/response; it is fan-out push. Comlink models this via proxied callbacks whose lifetimes must be manually `releaseProxy`'d — leak-prone across `BroadcastChannel` and across tab close. A plain `{ t: 'push', topic, payload }` frame is trivially correct.
- **Extensible taxonomy.** ENG-79/81 add message types; an explicit discriminated union + handler registry gives compile-time exhaustiveness (`noFallthroughCasesInSwitch` is on) and structured-clone-safety by construction. Comlink hides the wire and would let a non-cloneable value through to a runtime `DataCloneError`.
- **Zero dep, full control**, and the wire form is exactly what we can log/inspect in the sync simulation later.

Comlink's ergonomics win only for the simplest single-`MessagePort` RPC — which is not our shape. Hand-rolled.

### D-3 · The transport-agnostic core — the testability seam (the key ruling)

The hard part of this ticket is that `SharedWorker`, `navigator.locks`, and `BroadcastChannel` are **not in jsdom/node**, and a `SharedWorker` global (`onconnect`) is not unit-testable. **Rule: all worker LOGIC lives in a pure, transport-agnostic `WorkerCore` class** that takes **(1) a database handle** and **(2) a `MessageSink`** — nothing else. It never references `self`, ports, channels, or locks.

```ts
// worker/core.ts
export type MessageSink = (clientId: string, msg: FromWorker) => void

export class WorkerCore {
  constructor(private db: MsgDb, private sink: MessageSink) {}
  register(method: RpcMethod, handler: RpcHandler): void   // ENG-79/80/81 extend here
  async handle(clientId: string, msg: ToWorker): Promise<void>  // routes → registered handler → replies via sink
  publish<T extends Topic>(topic: T, payload: PushPayload<T>): void  // fan-out to subscribers via sink
  evictStream(streamId: string): Promise<void>             // bounded-cache stub (D-6)
}
```

- **`MsgDb`** (see D-4) is the database surface `WorkerCore` uses. In tests it is a real Dexie instance running on **`fake-indexeddb`** (an injected `IDBFactory`, not the global `/auto`, to stay hermetic); in prod-degraded it is the Map-backed store; in prod it is Dexie on real IndexedDB. Same code path.
- **`MessageSink`** is the only output. In the SharedWorker adapter the sink writes to the right connection `port`; in the leader adapter it posts on the `BroadcastChannel` addressed to `clientId`; **in tests it is a fake that collects frames into an array**. This one seam makes `WorkerCore` — and therefore ENG-79's sync engine and ENG-80's projections that will register handlers on it — fully unit-testable **without a browser**.

The transports (`shared-worker.ts`, `leader.ts`) are **thin adapters**: they own the platform APIs, translate incoming platform messages into `core.handle(clientId, msg)`, and provide a `sink` that serialises out. They carry **no business logic**, so the fact that they can only be smoke-tested in Playwright (ENG-83) costs us nothing.

### D-4 · Dexie schema (§5.2, verbatim) + persistence abstraction

The schema is copied exactly from TDD §5.2 — indexes are load-bearing and must not drift:

```ts
// worker/db.ts
export const PROJECTION_VERSION = 1

this.version(1).stores({
  events:     '[stream_id+server_sequence], event_id, type',              // raw envelopes (cache, evictable)
  messages:   'message_id, stream_id, [stream_id+created_seq], thread_root_id',
  streams:    'stream_id, kind',                                          // + name, visibility, head_seq, member
  cursors:    'stream_id',                                                // + last_contiguous_seq, oldest_loaded_seq
  outbox:     'event_id, created_at',                                     // + body, state: queued|sending|rejected
  read_state: 'stream_id',                                                // + last_read_seq (local echo of server KV)
  meta:       'key',                                                      // projection_version, session info, my user_id
})
```

Only the fields listed after the schema string are indexed; the non-indexed fields (row shape) are declared as **TS row interfaces** in `types.ts` and enforced by strict typing, not by Dexie:

- `streams`: `{ stream_id, kind, name?, visibility?, head_seq, member }`
- `cursors`: `{ stream_id, last_contiguous_seq, oldest_loaded_seq }`
- `outbox`: `{ event_id, created_at, body, state: 'queued'|'sending'|'rejected' }`
- `read_state`: `{ stream_id, last_read_seq }`
- `events`: `{ stream_id, server_sequence, event_id, type, /* full envelope */ ... }`
- `meta`: `{ key, value }` (rows include `key: 'projection_version'`, `'my_user_id'`, `'session'`, …)

**Two independent version numbers — ruled and documented:**

- **Dexie `version()`** governs the *IndexedDB index layout*. Any change to a `.stores()` string bumps this and adds a Dexie `.upgrade()`. Ships at `version(1)` now.
- **`PROJECTION_VERSION`** (app-level, stored in `meta['projection_version']`) governs *derived-table validity*. Bumping it does **not** touch the IndexedDB schema; it forces a rebuild of derived tables from the raw `events` cache.

**Derived vs. source tables** (the drop set): derived = `messages`, `streams`, `cursors`, `read_state` (all are projections/echoes rebuildable from `events` + server pulls). Source-of-truth-ish = `events` (raw envelope cache) and **`outbox`** (pending local sends — never derived, never dropped, never evicted).

**PROJECTION_VERSION plumbing (this ticket):** on DB open, `WorkerCore` reads `meta['projection_version']`. On mismatch (or missing): clear the derived tables (`messages`, `streams`, `cursors`, `read_state`) in one transaction, call `rebuildProjections()` — **a stub here** (logs + writes the new `projection_version`; the real replay from `events` is ENG-80) — then proceed. A unit test asserts the mismatch path (a) drops derived tables and (b) leaves `events` and `outbox` intact.

**`MsgDb` persistence abstraction (satisfies D-3 + D-5):** `WorkerCore` depends on a **structural `MsgDb` interface** — the exact set of DB operations the worker uses (at ENG-77: `meta` get/put, `events` range read for eviction, `outbox` read, derived-table clear, table `count`). Two implementations:

- **`DexieDb`** — wraps the Dexie subclass. Used in prod (real IndexedDB) and in tests (Dexie constructed with an injected `fake-indexeddb` `IDBFactory`).
- **`MemoryDb`** — Map-backed, no persistence. Used in the degraded path (D-5) and as the fastest unit-test double.

The interface is small now and **grows as ENG-79/80/81 add queries** (each new read/write is added to `MsgDb` + both impls). This is a deliberate, documented tax: it is exactly what keeps the sync engine and projections unit-testable against either backend. Dexie's richer fluent query API is reachable inside `DexieDb`; callers stay on the interface.

### D-5 · Graceful degradation — detection then Map-backed fallback

`openDb()` in `db.ts` is the single boot point:

1. **Detect** by opening a tiny probe Dexie DB and awaiting it. Private-browsing / disabled-IDB throws (`SecurityError`, `InvalidStateError`, or an immediate `blocked`/quota error). Wrap in try/catch with a short timeout guard (some engines hang the open).
2. **Success** → construct the real `MsgDB` (Dexie) and return `new DexieDb(msgdb)`, status `db: 'persistent'`.
3. **Failure** → return `new MemoryDb()`, status `db: 'memory'`. Everything still works online; nothing persists across reload (acceptable — TDD §5.2: "in-memory cache, everything still works online").

`fake-indexeddb` stays **dev-only**; the prod degraded path is the hand-rolled `MemoryDb`, so no test shim ships in the bundle. The chosen `MsgDb` is handed to `WorkerCore`; nothing downstream branches on which one it is.

### D-6 · Bounded-cache eviction stub

`WorkerCore.evictStream(streamId)` — documented + a **simple, correct implementation**, not just a no-op: keep the newest ~2,000 rows in `events` for a stream (by the `[stream_id+server_sequence]` index, descending), delete older. **It queries `events` only and can never reach `outbox`** (different table; the method has no `outbox` handle). `MAX_CACHED_EVENTS_PER_STREAM = 2000` is a named constant. It is **not wired into a hot path** this ticket (there is no apply loop until ENG-79); it ships with a direct unit test (seed 2,500 events + some outbox rows → assert ≤ 2,000 events remain, newest kept, outbox untouched). Whether eviction runs post-apply or on a timer is ENG-79/80's call; the hook exists and is proven safe here.

### D-7 · RPC message taxonomy (extensible)

Top-level frames are discriminated unions, structured-clone-safe (plain data only; a dev-mode `assertCloneable()` guards payloads):

```ts
// worker/types.ts
// Tab → Worker
export type ToWorker =
  | { t: 'hello'; clientId: string }                         // handshake / registers the tab
  | { t: 'req';   id: string; clientId: string; req: RpcRequest }  // id = correlation id
  | { t: 'sub';   id: string; clientId: string; topic: Topic }
  | { t: 'unsub'; id: string; clientId: string }
  | { t: 'bye';   clientId: string }                         // tab disposing

// Worker → Tab
export type FromWorker =
  | { t: 'res';    id: string; ok: true;  result: unknown }
  | { t: 'res';    id: string; ok: false; error: RpcError }
  | { t: 'push';   topic: Topic; payload: unknown }
  | { t: 'status'; status: WorkerStatus }

// RpcRequest is itself an extensible union on `method` (ENG-79/81 add members):
export type RpcRequest =
  | { method: 'meta.get';  params: { key: string } }         // stub read, ships now
  | { method: 'query';     params: QueryParams }             // QueryParams: stub union, ENG-80 extends
  | { method: 'mutate';    params: MutateParams }            // MutateParams: stub union, ENG-81 extends
  | { method: 'ping';      params: Record<string, never> }

export type Topic =                                          // ENG-79/80 add topics
  | { kind: 'stream'; stream_id: string }                    // projection updates for a stream
  | { kind: 'status' }                                       // transport/db status changes

export interface RpcError { code: string; detail?: string }
```

Taxonomy = **four verbs** — `query` (read a projection), `mutate` (enqueue a durable event / set read-state), `subscribe` (register for pushes), `event-push` (worker→tab fan-out) — plus control frames (`hello`/`bye`/`ping`/`status`). `RpcMethod`, `QueryParams`, `MutateParams`, and `Topic` are the **extension points**; a new capability = a new union member + a `core.register(method, handler)` call, transports untouched. **Correlation:** every `req`/`sub` carries a client-generated `id` (`crypto.randomUUID()`); `rpc.ts` keeps a `Map<id, {resolve, reject, timeout}>` and matches `res` frames back, with a configurable request timeout → `reject`. **Addressing:** every frame carries `clientId` so the leader's `BroadcastChannel` can fan responses to the right follower and ignore its own echoes.

**ENG-77 handlers are stubs:** `meta.get` (real — reads `meta`), `ping` (real — returns `{ pong: true }`), `query`/`mutate` registered but returning `{ code: 'not_implemented' }` for as-yet-undefined discriminants. This proves the full round trip (AC #1) end-to-end while leaving the behaviour to later tickets.

---

## Files

All new, all **ui-engineer**. Under `web/src/worker/`:

| File | Contents |
|---|---|
| `types.ts` | Protocol unions (`ToWorker`/`FromWorker`/`RpcRequest`/`Topic`/`RpcError`), `WorkerClient` + `WorkerStatus` + `Unsubscribe`, `MsgDb` interface, `MessageSink`, all Dexie **row interfaces**, `PROJECTION_VERSION` + `MAX_CACHED_EVENTS_PER_STREAM` consts. Pure types + consts, no runtime deps — importable by both tab and worker sides. |
| `db.ts` | `MsgDB extends Dexie` with the §5.2 `.stores()` **verbatim**; `openDb()` (detection → `DexieDb` \| `MemoryDb`, D-5); `DexieDb` and `MemoryDb` implementing `MsgDb`; `clearDerivedTables()`; `checkProjectionVersion()` + `rebuildProjections()` **stub** (D-4). |
| `core.ts` | `WorkerCore` (D-3): handler registry, `handle()`, `publish()`, subscription tracking (`Map<clientId, Set<Topic>>`), `evictStream()` stub (D-6), and registration of the ENG-77 stub handlers. Zero platform globals. |
| `rpc.ts` | Correlation-id request/response plumbing: `createRpcCaller(postFn, onFrame)` returning `{ request, subscribe, dispose }`; pending-request map + timeouts; `assertCloneable()` dev guard. Shared by both transports (client side). |
| `client.ts` | `createWorkerClient(): Promise<WorkerClient>` — feature-detect → build `SharedWorkerTransport` \| `LeaderTransport` \| solo; wires `rpc.ts`; returns the single `WorkerClient` the stores use (D-1). |
| `leader.ts` | Web Locks election (`navigator.locks.request('msg-worker-leader', { mode:'exclusive' }, …)` held for the tab's life) + `BroadcastChannel('msg-worker')` transport; leader hosts a `WorkerCore` in-page with a channel-writing `MessageSink`; followers proxy; handoff on leader tab close. |
| `shared-worker.ts` | The `SharedWorker` **entry** (`onconnect` → per-tab `port`; one shared `WorkerCore` + `MsgDb`; each port gets a sink; `port` messages → `core.handle`). Thin adapter, no logic. This is the `new SharedWorker(new URL('./shared-worker.ts', import.meta.url), { type:'module' })` target already pre-wired by ENG-75. |
| `index.ts` *(optional)* | Barrel re-exporting `createWorkerClient` + public types for `stores/` (ENG-82). |

Under `web/tests/unit/worker/`:

| File | Contents |
|---|---|
| `helpers.ts` | Fake `MessageSink` (collects frames), in-memory `IDBFactory` builder from `fake-indexeddb`, fake `navigator.locks` + `BroadcastChannel` harness for leader tests. |
| `db.spec.ts` | §5.2 schema shape; `openDb` persistent path (fake-indexeddb) + degraded path (force open failure → `MemoryDb`); `PROJECTION_VERSION` mismatch drops **only** derived tables, preserves `events`+`outbox`. |
| `core.spec.ts` | `WorkerCore.handle` round trips (`ping`, `meta.get`) against both `DexieDb`(fake-idb) and `MemoryDb`; `publish` reaches only subscribed clients; `evictStream` keeps newest 2,000 and never deletes `outbox` (D-6). |
| `rpc.spec.ts` | Correlation matching, out-of-order responses, timeout → reject, `assertCloneable` rejects functions. |
| `leader.spec.ts` | With faked locks+channel: one tab wins the lock (leader), a follower's `req` routes through the channel to the leader's `WorkerCore` and back (AC #3 — simulate no SharedWorker); leader release → next waiter promotes. |
| `client.spec.ts` | Feature detection selects the right transport (stub `SharedWorker` present vs. absent-with-locks vs. neither→solo); `WorkerClient` surface identical across them via a fake transport. |

`web/package.json`: add `"dexie": "^4.x"` to `dependencies`; `"fake-indexeddb": "^6.x"` to `devDependencies`. Commit the updated `pnpm-lock.yaml`. Node 22 provides `BroadcastChannel`, `structuredClone`, `crypto.randomUUID` globally; `navigator.locks` is faked in tests. Vitest `test.include` (`tests/unit/**/*.spec.ts`) already covers the nested `worker/` dir — no config change.

---

## Steps

1. **Deps + types.** Add `dexie` + `fake-indexeddb`, refresh lockfile. Write `types.ts`: protocol unions, `WorkerClient`, `MsgDb`, row interfaces, consts. Nothing imports platform globals.
2. **DB layer.** `db.ts`: `MsgDB` with §5.2 verbatim; `DexieDb`/`MemoryDb`/`MsgDb`; `openDb()` detection; `clearDerivedTables` + `checkProjectionVersion` + `rebuildProjections` stub. Land `db.spec.ts`.
3. **Core.** `core.ts`: `WorkerCore` with registry, `handle`, `publish`, subscription map, `evictStream` stub, ENG-77 stub handlers. Land `core.spec.ts` against both backends.
4. **RPC plumbing.** `rpc.ts`: correlation caller + `assertCloneable`. Land `rpc.spec.ts`.
5. **SharedWorker adapter.** `shared-worker.ts` entry; verify it builds under `worker: {format:'es'}` (`pnpm build`).
6. **Leader adapter.** `leader.ts`: Web Locks election + BroadcastChannel transport + in-page `WorkerCore` host + handoff. Land `leader.spec.ts` with the faked harness.
7. **Client factory.** `client.ts`: feature detection + assembly returning `WorkerClient`. Land `client.spec.ts`.
8. **Gate.** `pnpm typecheck && pnpm lint && pnpm test && pnpm build` green. (Optional) a throwaway two-tab manual check in dev; real multi-tab is ENG-83 Playwright.

---

## Acceptance criteria mapping

- *Tab opens worker, issues RPC, gets typed response; multiple tabs share one worker/leader* → D-1 + `client.spec.ts`/`core.spec.ts` (round trip), `leader.spec.ts` (shared leader).
- *Dexie schema matches §5.2 exactly; `PROJECTION_VERSION` bump path stubbed* → D-4 + `db.spec.ts`.
- *Leader-election fallback exercised in a test (simulate no SharedWorker)* → D-1 + `leader.spec.ts`.
- *IndexedDB-absent path degrades to in-memory without crashing* → D-5 + `db.spec.ts` degraded case.

---

## Risks & open questions

1. **Web Locks handoff / split-brain.** If election is mis-implemented two tabs could both host a `WorkerCore` (double writers — the exact bug SharedWorker avoids). Mitigation: the leader holds a single exclusive lock for its lifetime; promotion happens only inside the lock callback; `leader.spec.ts` asserts exactly one leader across a simulated handoff. **Highest-risk item.**
2. **`fake-indexeddb` ≠ Safari IDB.** Compound-index (`[stream_id+server_sequence]`) semantics can differ subtly from a real engine. Unit tests prove logic; ENG-83 Playwright is the real-browser backstop. Low but noted.
3. **`MsgDb` interface tax.** Forcing ENG-79/80/81 through the interface (vs. raw Dexie fluency) costs some ergonomics. Accepted: it is the price of the dual backend + unit-testability, and each ticket extends the interface as it needs. Documented so later tickets expect it.
4. **Structured-clone violations.** A row carrying a class instance/function would throw `DataCloneError` only at runtime across a transport. Mitigation: rows are plain data by type; `assertCloneable()` dev guard in `rpc.ts`.
5. **SharedWorker + ES-module + HMR in dev.** Vite serves module workers fine, but HMR can leave stale worker instances; a hard reload clears it. Dev-only friction, no prod impact.
6. **BroadcastChannel fan-out noise.** Every follower sees every frame; addressing by `clientId` (D-7) keeps it correct, and at 5–50-user / few-tab scale volume is trivial.
7. **Degenerate solo fallback** (no SharedWorker *and* no Web Locks) gives no cross-tab coherence. Acceptable for the long tail; documented in `client.ts`.

---

## Agent assignments

**All ui-engineer.** No server / cli / devops work: the existing `web` CI job (ENG-75) already runs typecheck + lint + test + build over the new files with no config change.
