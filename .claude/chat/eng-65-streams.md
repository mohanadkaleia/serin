# ENG-65 — M1: Streams, membership, and the workspace-meta stream

**Ticket:** ENG-65 (M1). **Role:** tech-lead planning. **Implementers:** `python-engineer` (all of it — `server/` only).
**Depends on:** ENG-63 (schema: `streams`, `stream_members`, `events`), ENG-64 (auth layer, `AuthContext`, `require_auth`/`require_role`, problem+json, `RateLimiter`, harness savepoint isolation + committing fixtures).
**Blocks:** ENG-66 (event upload endpoint), ENG-73 (docs/schemas + vectors), sync/search tickets (predicate).

---

## 0. Scope ruling (read first)

ENG-65 builds the **machinery the ENG-66 upload endpoint will call**, not the endpoint. Concretely it ships:

1. `insert_event` — the minimal server-side event-insert primitive (sequence assignment + row insert + hash).
2. Meta-event **reducers** — a registry mapping accepted meta types to idempotent state mutations on `streams`/`stream_members`.
3. `core/payloads/meta.py` — Pydantic payload models for the `workspace-meta` / channel-lifecycle event types.
4. The **readable-streams predicate** (one shared SQL fragment) + `can_read`/`can_write` helpers + a `require_readable_stream` 404-dependency.
5. The **write-permission matrix** (documented + a `can_write` predicate for ENG-66 to enforce).
6. Two **seam fills** in the ENG-64 auth router: `workspace.created` (+ owner `user.joined`) on `/v1/setup`, and `user.joined` on `/v1/auth/accept-invite`.

**Explicitly NOT in ENG-65** (keep this the smallest reviewable unit):
- No `POST /v1/events/batch` upload endpoint (ENG-66). `insert_event` is the primitive ENG-66 wraps in the full §3.2 validation pipeline.
- **No new HTTP endpoints at all.** Channel CRUD is *not* an API — channels are born from `channel.created` events uploaded via ENG-66. The only HTTP-visible changes are the two seam fills inside existing endpoints.
- No DM-creation endpoint. DM streams are lazy-on-first-message (M3). ENG-65 ships the `dm.created` reducer + predicate support so the machinery is ready; the endpoint is deferred and documented.
- No message projection (`messages_proj`) — `message.created` has **no reducer** in this ticket.
- No migration — `streams`, `stream_members`, `events` already exist (ENG-63, §4.2). This ticket writes zero DDL.

---

## Implementation Plan

### D1 — `insert_event`: the event-insert primitive

Home: `server/msgd/events/insert.py`. Signature (async, session-scoped, **does not commit**):

```python
async def insert_event(db: AsyncSession, *, stream_id: str, body: dict[str, Any]) -> Envelope
```

Steps, all inside the caller's transaction:

1. **Hash the raw body.** `event_hash = hash_event(body)` — over the raw dict, per the ENG-56 discipline (never `model_dump` of a re-parsed model; here the server *is* the source of truth so the dict passed in is authoritative and stored verbatim). Propagate `JCSError` to the caller.
2. **Assign the sequence** with the TDD §3.1 canonical statement:
   ```sql
   UPDATE streams SET head_seq = head_seq + 1 WHERE stream_id = :sid RETURNING head_seq
   ```
   This is a single atomic statement that takes a row-level write lock on the `streams` row — functionally identical to the ticket's "`SELECT … FOR UPDATE` on `streams.head_seq` then bump", but one round trip and exactly the form §3.1/§4.2 pin. **Ruling: use `UPDATE … RETURNING`, not a separate `SELECT FOR UPDATE`.** If the row does not exist (0 rows returned) raise a typed `UnknownStreamError` — callers must have bootstrapped the row first (see D4).
3. **Insert the `events` row** with the returned `server_sequence`, pulling scalar columns from `body` (`workspace_id`, `event_id`, `type`, `type_version`, `author_user_id`, `author_device_id`), `client_created_at` **parsed** from the body's RFC3339 string into a `datetime` (lossy/untrusted convenience column per the `Event` HASH-INVARIANT docstring — never re-hashed), `server_received_at = now()`, `event_hash`, `payload_redacted = False`, and `body = body` (verbatim JSONB — the sole hash source).
4. **Return** an `Envelope(body=Body(**body), event_hash=…, signature=None, server=ServerMetadata(server_sequence, server_received_at, payload_redacted=False))` so the caller (and later ENG-66's response shaping) has the stored envelope in hand.

**What `insert_event` deliberately does NOT do** (ENG-66 owns these): schema validation, `event_hash` *recomputation-and-compare* against a client-supplied hash, author-matches-session check, referential checks, size cap, and **idempotency by `event_id`**. It assumes a fresh, server-trusted body — true for both M1 callers (the two server-authored events have freshly minted `event_id`s). ENG-66 wraps `insert_event` with the §3.2 pipeline and the `UNIQUE(workspace_id, event_id)` idempotent-return path.

**Concurrency correctness:** the per-stream `UPDATE … RETURNING` row lock serializes concurrent inserts to the same stream → gapless, monotonic `head_seq` (D2). Proven by a committing-fixtures concurrency test (D9 test plan).

`client_created_at` for **server-authored** bodies needs an RFC3339 string. The CLI's `now_rfc3339` lives in `cli/` (server cannot import it). **Ruling: add `now_rfc3339()` to `msgd/core/` (a 3-line UTC formatter next to `_validate_rfc3339` in `envelope.py`, or a tiny `core/time.py`) and have the CLI re-export it later if desired.** Keeps the timestamp helper on the shared side.

### D2 — Server-authored-event identity ruling

Server-authored meta events must produce **hash-valid** envelopes (real typed ids that pass `Body`'s validators; `signature=null`). Per the §2.2 table, **the acting user authors the event**:

| Event | `author_user_id` | `author_device_id` | Home stream | Seq |
|---|---|---|---|---|
| `workspace.created` | the owner (setup user) | owner's just-minted device | workspace-meta | 1 |
| `user.joined` (owner, at setup) | the owner | owner's device | workspace-meta | 2 |
| `user.joined` (invitee, at accept) | the joining user | invitee's just-minted device | workspace-meta | next |

**Ruling: `/v1/setup` emits BOTH `workspace.created` (seq 1) then `user.joined` for the owner (seq 2).** This makes "*every workspace member has exactly one `user.joined` in the meta log*" a uniform invariant — the client member-list projection has one code path, no "owner is implicit" special case. `channel.*` and `dm.*` meta events are **client-authored** (uploaded via ENG-66), so ENG-65 server-authors only `workspace.created` and `user.joined`; the reducers, however, cover the full meta-type set.

Because the server constructs these bodies from Pydantic models via `model_dump(mode="json")` (source of truth is the model), `hash_event(stored_body) == event_hash` holds exactly (the ENG-56 lax-coercion hazard does not apply on the construction path). Server-authored builders live in `core/payloads/meta.py` (`build_workspace_created_body`, `build_user_joined_body`), mirroring `build_message_created_body`.

### D3 — Meta-event reducers

Home: `server/msgd/events/reducers.py`. A reducer is a **pure function of `(event body dict, db state)`** that mutates `streams`/`stream_members`, **runs in the same transaction as the event insert**, and is **idempotent under replay** (ENG-66's rebuild replays the stored log and re-runs reducers). Contract:

```python
Reducer = Callable[[AsyncSession, dict], Awaitable[None]]
REDUCERS: dict[str, Reducer]              # keyed by event `type`
async def apply_reducer(db, body) -> None # dispatch; no-op for types with no reducer
```

Idempotency mechanics: stream/member creation is `INSERT … ON CONFLICT DO NOTHING`; renames/archives/removes are deterministic `UPDATE`/`DELETE`. Re-running any reducer over already-applied state is a no-op.

| Event type | Reducer effect |
|---|---|
| `workspace.created` | Ensure the workspace-meta `streams` row exists (`INSERT … ON CONFLICT DO NOTHING`, `kind='workspace-meta'`, `visibility=NULL`), keyed on `body.stream_id`. |
| `user.joined` / `user.left` / `user.profile_updated` | **No-op** on `streams`/`stream_members` (the `users` row is authored by the auth handler; workspace-meta readability is by workspace role, not a `stream_members` row — see D5). Registered as explicit no-ops so the dispatch table is total. |
| `channel.created` | `INSERT streams(stream_id=payload.channel_stream_id, kind='channel', name, visibility) ON CONFLICT DO NOTHING` (`head_seq` defaults 0) **+** `INSERT stream_members(payload.channel_stream_id, body.author_user_id) ON CONFLICT DO NOTHING` (creator is subscribed). |
| `channel.renamed` | `UPDATE streams SET name=payload.name WHERE stream_id=payload.channel_stream_id`. |
| `channel.archived` | `UPDATE streams SET archived_at=now() WHERE stream_id=…` (archived streams stay readable; only writes/UI change). |
| `channel.member_added` | `INSERT stream_members(payload.channel_stream_id, payload.user_id) ON CONFLICT DO NOTHING`. |
| `channel.member_removed` | `DELETE FROM stream_members WHERE stream_id=payload.channel_stream_id AND user_id=payload.user_id`. |
| `dm.created` | `INSERT streams(payload.dm_stream_id, kind='dm', visibility=NULL) ON CONFLICT DO NOTHING` **+** one `stream_members` row per `payload.member_user_ids` (`ON CONFLICT DO NOTHING`). |
| `message.created` | **No reducer in ENG-65** (message projection is ENG-66+). |

`bot.installed` / `bot.removed` are M5 — not registered.

### D4 — Private-channel bootstrap ruling: reducer-before-insert

**The paradox:** `insert_event` locks the `streams` row to assign a sequence, so the row must exist *before* the first event is sequenced into it. But for a **private** channel (and a DM), §2.2 says the genesis `channel.created`/`dm.created` event lives in **the new stream's own stream at sequence 1** (self-describing). The stream cannot host its own genesis event unless its row already exists.

**Ruling — the orchestrator runs the reducer BEFORE `insert_event`, in one transaction:**

```python
async def emit_event(db, *, home_stream_id: str, body: dict) -> Envelope:
    await apply_reducer(db, body)                       # ensures streams rows + members (head_seq stays 0)
    return await insert_event(db, stream_id=home_stream_id, body=body)  # locks the now-existing row → seq
```

This one ordering is uniform and correct for every meta type:

- **`workspace.created`** → reducer ensures the workspace-meta row (from `body.stream_id`); insert → seq 1 in it. `home_stream_id == body.stream_id`.
- **Public `channel.created`** → `home_stream_id = workspace-meta` (already exists); reducer creates the channel's *own separate* stream row (`payload.channel_stream_id`, `head_seq=0`) + creator membership; insert appends to workspace-meta's sequence. The channel's own stream is now ready for future messages.
- **Private `channel.created`** → `home_stream_id == payload.channel_stream_id == body.stream_id`; reducer creates that row (`head_seq=0`); insert → **seq 1 in the channel's own stream** (self-describing). ✓
- **`dm.created`** → `home_stream_id == payload.dm_stream_id`; reducer creates the DM row + member rows; insert → seq 1 in the DM stream.
- **`channel.member_added/removed`, `renamed`, `archived`** → home stream already exists (workspace-meta for public lifecycle, the channel's own stream for private lifecycle per §2.2); order is immaterial; reducer mutates membership/name.

Because the reducer owns *all* streams/members creation, an ENG-66 rebuild (re-run reducers over the stored log, no `insert_event`) reconstructs the full `streams`/`stream_members` state from event bodies alone — the required replay property.

**§2.2 privacy placement (which stream a lifecycle event lands in) is decided by the caller/uploader, not the reducer** (ENG-66 routes private-channel lifecycle events into the private channel's own stream; public + workspace-level events into workspace-meta). ENG-65 encodes this only in the two server-authored callers and documents it for ENG-66.

### D5 — Readable-streams predicate + read/write helpers

Home: `server/msgd/events/permissions.py`. **One shared SQL fragment** reused verbatim by pull (`/v1/events`, `/v1/sync`), search (§8), and WS fanout scoping.

```python
def readable_streams_predicate(*, user_id: str, role: str, workspace_id: str) -> ColumnElement[bool]
```

Returns a SQLAlchemy boolean over the `streams` table:

```
streams.workspace_id == workspace_id
AND (
    (streams.kind == 'workspace-meta' AND role != 'guest')                          -- meta: all non-guest members
 OR (streams.kind == 'channel' AND streams.visibility == 'public' AND role != 'guest')  -- public channels: all non-guest
 OR EXISTS (SELECT 1 FROM stream_members m                                          -- private / dm / guest-explicit
             WHERE m.stream_id = streams.stream_id AND m.user_id = user_id)
)
```

**Rulings baked in:**
- **`workspace-meta` is readable by non-guest members only** (owner/admin/member). Guests see *only* streams with an explicit `stream_members` row (§3.6). This is the precise reading of "workspace-meta readable by every member" — a guest is a member with restricted scope, and giving guests the meta stream would leak the full public-channel + member roster. Documented deviation from a naive "everyone subscribed" reading.
- **Public channels** are readable by every non-guest member without a membership row (§3.6: reading a public channel does not require joining). Guests need an explicit row (the `EXISTS` branch covers them).
- **Private channels + DMs** require a `stream_members` row (the `EXISTS` branch) — for *every* role.
- **Archived channels stay readable** (no `archived_at` filter in the predicate; archival gates writes/UI, not history access, D13).

Because the private/DM/guest branch is a live `EXISTS` on `stream_members`, **deleting a member row cuts predicate access on the very next query** — the "removal cuts server-side history access immediately" property (D13), tested directly.

Row-level helpers built on the same predicate (no divergent second implementation):
- `async def can_read(db, *, ctx, stream_id) -> bool` — `SELECT 1 FROM streams WHERE stream_id=:sid AND <predicate>`.
- `require_readable_stream` — a **FastAPI dependency** (placed in `api/deps.py` next to `require_auth`/`require_role`; it takes `stream_id` from the path/query + `CurrentAuth` + session) that raises `problems.not_found()` when the stream is missing **or** unreadable — the **404-not-403 discipline** (existence is not disclosed; unknown stream and forbidden stream return the identical `/problems/not-found` body).

### D6 — Write-permission matrix (§3.6 point 1; for ENG-66)

`can_write` in `permissions.py` — a predicate ENG-66 enforces at upload (enforcement point 1). ENG-65 provides + tests it but wires it to **no** endpoint. Server-authored events (D2) bypass it (server is authoritative).

```python
async def can_write(db, *, ctx: AuthContext, stream_id: str, event_type: str) -> bool
```

**M1 matrix (minimal, documented; loosen in later tickets — D13):**

| Event type | Who may write |
|---|---|
| `message.created` | Any member with **read/write access to the target stream** (public channel: any non-guest; private channel / DM: `stream_members` row). Reuses the D5 predicate — write access == read access for messages in M1. |
| `channel.created` | Any **non-guest** member (owner/admin/member). Guests cannot create channels. |
| `channel.renamed` / `channel.archived` | **owner/admin only.** M1 stores no per-channel "creator" role, so "creator may archive" is deferred — simplest safe rule is workspace admin/owner. Documented. |
| `channel.member_added` / `channel.member_removed` | **owner/admin only** for M1 (member-initiated invites to private channels are a deferred product call). Documented. |
| `dm.created` | Deferred with the DM endpoint (M3). Reducer + predicate ready; `can_write` returns a documented deferral (treat as owner/admin-not-applicable in M1 — no caller). |
| `message.edited` / `message.deleted` / `reaction.*` / `pin.*` / `file.uploaded` | **Out of ENG-65 scope** — ENG-66+ defines author-only/admin rules. Not in this matrix. |

### D7 — `core/payloads/meta.py`

New module beside `message.py`, same discipline as `MessageCreatedV1`: `model_config = ConfigDict(extra="allow")` (additive-only round-trip, §2.3), **format-validation only** (typed-id prefix + ULID validity; visibility `Literal["public","private"]`) — *not* referential existence (server concern). Models:

- `WorkspaceCreatedV1` — `name: str`.
- `UserJoinedV1` / `UserLeftV1` — `user_id` (valid `u_`), `display_name: str | None = None`.
- `UserProfileUpdatedV1` — `user_id` + changed fields (open via `extra="allow"`).
- `ChannelCreatedV1` — `channel_stream_id` (valid `s_`), `name: str`, `visibility: Literal["public","private"]`.
- `ChannelRenamedV1` — `channel_stream_id`, `name`.
- `ChannelArchivedV1` — `channel_stream_id`.
- `ChannelMemberAddedV1` / `ChannelMemberRemovedV1` — `channel_stream_id`, `user_id` (valid `u_`).
- `DmCreatedV1` — `dm_stream_id` (valid `s_`), `member_user_ids: list[str]` (each valid `u_`).

Register each `(type, version)` in `core/payloads/__init__.py::PAYLOAD_MODELS`, and add `build_workspace_created_body` + `build_user_joined_body` (server-authored body builders, mirroring `build_message_created_body`).

**CROSS-CUTTING FLAG:** `core/` is shared protocol surface (server + CLI + web). The JSON-Schema mirror in `docs/schemas/` **and** cross-language test vectors for these meta types are an **ENG-73 / M1-exit** concern, **not** ENG-65. Flag it in the ticket so ENG-73 picks up the new types; ENG-65 ships only the Pydantic models + registry entries.

### D8 — Seam fills in `routers/auth.py`

**`/v1/setup`** (replace the `# ENG-65 seam` comment at line ~111): after the owner `User` is flushed, **mint the device first** (reorder — the device must exist before authoring events, since `author_device_id` is validated), then:
1. `meta_stream_id = new_stream_id()`.
2. `emit_event(db, home_stream_id=meta_stream_id, body=build_workspace_created_body(...owner, device, meta_stream_id, name...))` → seq 1.
3. `emit_event(db, home_stream_id=meta_stream_id, body=build_user_joined_body(...owner...))` → seq 2.
4. Continue to `_login_response` (which commits — the meta stream + 2 events land atomically with the workspace/owner rows).

**`/v1/auth/accept-invite`** (replace the `# ENG-65 seam` comment at line ~263): after the invitee `User` flush succeeds and **after the device is minted** (reorder so the device exists), emit `user.joined` for the invitee into the workspace's meta stream. **Look up the meta stream** via `SELECT stream_id FROM streams WHERE workspace_id=:ws AND kind='workspace-meta'` (single-workspace MVP → exactly one). Then `_login_response` commits atomically.

No new problem types, no new response fields, no new endpoints — the two POST responses are byte-identical to ENG-64 (LoginResponse). This keeps the HTTP surface unchanged.

### File list

**New (`python-engineer`):**
- `server/msgd/events/__init__.py`
- `server/msgd/events/insert.py` — `insert_event`, `UnknownStreamError`.
- `server/msgd/events/reducers.py` — `REDUCERS`, `apply_reducer`, per-type reducers.
- `server/msgd/events/emit.py` — `emit_event` orchestrator (reducer→insert).
- `server/msgd/events/permissions.py` — `readable_streams_predicate`, `can_read`, `can_write`.
- `server/msgd/core/payloads/meta.py` — meta payload models + server-authored body builders.
- `server/msgd/core/time.py` (or extend `envelope.py`) — `now_rfc3339()`.
- `server/tests/test_insert_event.py`, `test_reducers.py`, `test_permissions.py`, `test_meta_payloads.py`, `test_setup_streams.py`.

**Modified:**
- `server/msgd/api/routers/auth.py` — fill both seams (D8); reorder device mint before event emit.
- `server/msgd/core/payloads/__init__.py` — register meta `(type, version)` models; export builders.
- `server/msgd/api/deps.py` — add `require_readable_stream` dependency.
- `server/tests/authutil.py` — add `streams, stream_members, events` to the committing-fixtures truncation list; small helpers to read stored events/streams.

**No** Alembic migration (tables exist). **No** `app.py` change (no new router).

### Test plan (pytest; `python-engineer`)

- **`insert_event` seq assignment / hash validity:** insert into a bootstrapped stream → `server_sequence` gapless from 1; assert **`hash_event(stored_body_jsonb) == stored.event_hash`** (raw-hash discipline: re-hash the verbatim stored dict, *not* `verify_hash` on a re-parsed model).
- **`insert_event` gapless under concurrency:** committing-fixtures app (real independently-committing sessions) + N concurrent inserts to one stream → sequences are `1..N` with no gaps/dupes (proves the `UPDATE … RETURNING` row lock). Truncate `events/streams/stream_members` after.
- **Reducer idempotency:** apply each reducer, snapshot `streams`/`stream_members`, re-apply the same body → byte-identical state (replay safety).
- **Predicate matrix:** parametric over role × kind × membership (owner/admin/member/guest × workspace-meta/public/private/dm × member?/not) — assert `can_read` true/false per D5; assert `require_readable_stream` returns **404** (not 403) for both unknown and unreadable streams.
- **Revocation cuts access instantly:** member reads a private stream (true) → delete `stream_members` row → same predicate query now false, no caching.
- **`workspace.created` seq-1 on setup:** after `/v1/setup`, the meta stream exists, `channel`/`dm` none; event at seq 1 is `workspace.created` (author=owner), seq 2 is `user.joined` (owner); `verify_hash`-green *and* `hash_event(stored_body) == event_hash`.
- **`user.joined` emission on accept:** after `/v1/auth/accept-invite`, a `user.joined` for the invitee (author=invitee) exists at the next meta sequence; hash green.
- **Private-channel bootstrap ordering:** drive `emit_event` for a private `channel.created` → the channel's own stream row exists and the event is at **seq 1 in that stream** (self-describing); public `channel.created` lands in workspace-meta while the channel's own stream sits at `head_seq=0`.
- **`can_write` matrix:** guest cannot `channel.created`; member can; member/guest cannot `channel.renamed`/`member_added` (admin/owner only); `message.created` follows stream read access.
- **`dm.created` reducer:** creates the DM stream + all member rows; predicate then admits exactly those members (endpoint deferred — reducer-only test).

---

## Rulings summary (for the summary-back)

1. **`insert_event`** = pure sequence+insert primitive: `hash_event(raw body)` → `UPDATE streams SET head_seq=head_seq+1 … RETURNING` (row-locked, gapless) → insert verbatim `events` row → return stored `Envelope`. No commit, no validation, no idempotency (ENG-66 wraps it).
2. **Server-authored identity:** acting user authors — `workspace.created` by the owner, `user.joined` by the joiner, using their just-minted device; setup emits `workspace.created`(1) + owner `user.joined`(2) for a uniform "every member has a `user.joined`" invariant.
3. **Private-channel bootstrap:** **reducer-before-insert** in one transaction — the reducer idempotently creates the streams row (head_seq=0), then `insert_event` sequences the self-describing genesis event as seq 1 in that stream. Uniform ordering across all meta types; enables replay-rebuild from bodies alone.
4. **Write matrix (M1):** `message.created` = stream read access; `channel.created` = any non-guest; `channel.renamed/archived/member_added/member_removed` = owner/admin; `dm.created` deferred. `can_write` provided for ENG-66; unwired here.
5. **Predicate:** one SQL fragment — meta & public readable by non-guest members, private/DM/guest via `EXISTS(stream_members)`; archived stays readable; live `EXISTS` gives instant revocation. `can_read`/`can_write` + `require_readable_stream` (404-not-403) on top.
6. **`core/payloads/meta.py`:** Pydantic models with `extra="allow"` + format-validation (same as `MessageCreatedV1`), registered in `PAYLOAD_MODELS`; docs/schemas + cross-language vectors are **ENG-73/M1-exit — flagged, not built here**.

## Risks / open questions

- **Reducer-before-insert is a subtle invariant** — a future engineer could "naturally" swap to insert-then-reduce and break private-channel genesis. Mitigate with a load-bearing comment in `emit.py` + the bootstrap-ordering test as the guard.
- **`now_rfc3339` placement:** server can't import the CLI helper; adding it to `core/` is the clean fix but touches shared surface — keep it a trivial pure formatter (no new deps).
- **Guest ↔ workspace-meta ruling** deviates from a literal "every member subscribed" reading of §2.2. It's the correct §3.6 interpretation, but flag it for the reviewer so it's a conscious call, and note it for the web member-list projection (guests won't receive meta).
- **`head_seq` lock contention** is per-stream and trivial at 5–50 users (§3.1); the concurrency test documents the guarantee. No global bottleneck.
- **Committing-fixtures cleanup:** the new concurrency test writes real rows to `events/streams/stream_members`; these MUST be added to `authutil.truncate_auth_tables` (or a parallel helper) or committed rows leak across tests.
- **ENG-66 coupling:** ENG-66 must (a) route private-channel lifecycle events into the private stream vs workspace-meta per §2.2, (b) enforce `can_write`, (c) wrap `insert_event` with idempotency + full validation. This division is documented above so ENG-66 inherits it rather than rediscovering it.
