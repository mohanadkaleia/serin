# msg

**Best-in-class team messaging where the workspace is portable, self-hostable, scriptable, and syncs like Git.**

msg is a local-first, file-based team messaging app for 5–50-person technical teams. The source of truth is an **append-only event log**; every rendering surface — server tables, client caches, search — is a **rebuildable projection** of it. A self-hosted sync server validates, sequences, stores, and fans out events; clients replicate per-stream and render locally.

> **Local-first *architecture* is universal. Local-first *storage* is per-platform.**
> The protocol (client-minted event IDs, per-stream cursors, idempotent upload, outbox, projection rebuild) applies to every client. The files (NDJSON logs, SQLite, "workspace is a folder") live on the server as the export format, and later on the desktop client.

## Status

**M0 — Protocol spike: complete** (tagged [`m0`](https://github.com/mohanadkaleia/msg/releases/tag/m0)). The event protocol is proven end-to-end in miniature: envelope schema locked, cross-language hash vectors frozen, and the rebuild ≡ incremental equivalence gate running permanently in CI.

**M1 — Sync server: complete** (tagged `m1`). msg is now a client-server system: token auth + sessions + single-use invites, streams + membership with a §3.6 read/write permission predicate, a `workspace-meta` stream, idempotent batch upload with gapless per-stream sequencing, pull/sync bootstrap, permission-scoped WebSocket fanout, Postgres + Alembic migrations, one-command compose self-host, and a simulation-suite skeleton (four of six §12 invariants) green in CI. The M1 exit gate proves two `msgctl` clients converge over the *real* server under interleaved bidirectional traffic.

| Milestone | Scope | Status |
|---|---|---|
| **M0 — Protocol spike** | `core/` envelope + JCS + hashing; `msgctl` append → project → rebuild → verify | ✅ Done |
| **M1 — Sync server** | Auth, streams, batch upload, sync, WebSocket fanout, Postgres | ✅ Done |
| M2 — Web client + sync proof | Vue shell, SharedWorker + Dexie, six invariants green in CI | Next |
| M3 — Messaging core | Threads, reactions, mentions, files, search, presence | — |
| M4 — Portability | `export` / `import` / `verify` round-trip | — |
| M5 — Plugins | Industry-standard incoming webhooks, bot tokens | — |
| M6 — Desktop (Tauri) | True offline; "workspace is a folder" | — |

Full design docs: [`docs/design-doc.md`](docs/design-doc.md) (product), [`docs/tech-lead-assessment.md`](docs/tech-lead-assessment.md) (pre-implementation review), [`docs/technical-design.md`](docs/technical-design.md) (the implementation contract — locked decisions D1–D14 live here).

## The protocol in one paragraph

Every event is an envelope with a client-authored `body` and server-assigned `server` metadata. `event_hash = sha256(JCS(body))` — SHA-256 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonicalization of the body only; the server never mutates an accepted body. All entity IDs are typed ULIDs (`w_`, `u_`, `s_`, `m_`, `f_`, `d_`), client-mintable and offline-safe. The server assigns a **gapless, monotonic `server_sequence` per stream**; a gap means data loss, not permissions. Uploads are idempotent by `event_id` — retries can never duplicate. Unknown event types are preserved in the log, skipped in projections, and never crash anything. The canonical JSON test vectors that every implementation (Python today, TypeScript at M2) must pass byte-for-byte are frozen in [`server/msgd/core/testdata/vectors.json`](server/msgd/core/testdata/vectors.json); JSON Schemas for the envelope and payloads are published in [`docs/schemas/`](docs/schemas/).

## Repository layout

```text
msg/
  server/
    msgd/
      core/           # event envelope, JCS canonicalization, hashing, payload schemas
        testdata/     # frozen cross-language hash vectors (47 cases)
      api/            # FastAPI app: auth, /v1/events(/batch), /v1/sync, /v1/ws
      db/             # SQLAlchemy models + Alembic migrations (Postgres)
      ws/             # WebSocket hub — permission-scoped fanout, heartbeat
      projections/    # server messages_proj: incremental apply + rebuild
    tests/            # core + api/db/ws tests, schema/vector freeze guards, simulation suite
  cli/
    msgctl/           # workspace CLI: init, send, project, verify, rebuild;
                      #   remote verbs: login, push, pull, invite
    tests/            # rebuild ≡ incremental gate + the M1 exit-gate E2E (integration)
  docs/
    schemas/          # published JSON Schemas (envelope + message.created & meta payloads)
    deploy.md         # self-hosted compose deployment guide
  docker-compose.yml  # one-command self-host: Postgres + the sync server
  .github/workflows/  # CI: ruff, mypy (strict), equivalence gate, pytest (incl. integration E2E), simulation suite
```

## Quickstart (M0: local workspace via `msgctl`)

The offline story — no server. Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# Create a workspace folder
uv run msgctl init ./demo

# Append messages (envelope built, hashed, sequenced, fsync'd — one NDJSON line per event)
uv run msgctl send ./demo --stream general --text "hello, world"
uv run msgctl send ./demo --stream general --text "second message"

# Materialize the SQLite projection incrementally (idempotent — run it twice)
uv run msgctl project ./demo

# Drop the projection and replay the whole log (temp DB + atomic swap)
uv run msgctl rebuild ./demo

# Recompute every hash, check gapless sequences, validate schemas
uv run msgctl verify ./demo            # human report
uv run msgctl verify ./demo --json     # machine-readable, CI-friendly exit codes
```

The workspace is just a folder: `workspace.json` plus `streams/<stream_id>/<YYYY-MM>.ndjson` — the same tree the M4 export format uses. `verify` is the ownership pitch made testable: it re-derives every `event_hash` from the stored bytes and proves per-stream sequence contiguity.

## Quickstart (M1: self-hosted server + `msgctl` remote)

The client-server story — one operator self-hosts the sync server; each teammate binds a local workspace to it and syncs.

**1. Bring up the server** (Postgres + the sync daemon) via Docker Compose. See [`docs/deploy.md`](docs/deploy.md) for the full guide.

```bash
cp .env.example .env          # fill in MSG_SECRET_KEY and the Postgres password
docker compose up -d          # starts Postgres + runs migrations + serves the API
curl -fsS http://localhost:8000/healthz   # -> {"status":"ok"}
```

**2. The owner sets up the workspace** and mints an invite for a teammate:

```bash
# First run: create the workspace + owner account (also creates the public `general`)
uv run msgctl login ./acme --setup \
  --server-url http://localhost:8000 \
  --email owner@example.com --password '…' \
  --workspace-name Acme --display-name Owner

uv run msgctl send ./acme --stream general --text "hello from the owner"
uv run msgctl push ./acme                       # upload queued events to the server

uv run msgctl invite ./acme --role member       # prints a single-use join URL
```

**3. A teammate joins** with the invite token and syncs:

```bash
uv run msgctl login ./bob --invite-token <token-from-the-join-url> \
  --server-url http://localhost:8000 \
  --email bob@example.com --password '…' --display-name Bob

uv run msgctl pull ./bob                          # mirror the server's streams locally
uv run msgctl send ./bob --stream general --text "hi, owner"
uv run msgctl push ./bob
uv run msgctl pull ./acme                          # owner sees Bob's message
```

`push` is only a hint that new events are queued; **cursors are the truth** — `pull` is idempotent and drives every client to the same gapless per-stream sequence the server assigned. Run `verify` on any synced workspace to re-derive every `event_hash` from the stored bytes, exactly as in M0. These are the same commands the M1 exit-gate E2E drives to prove two clients converge over the real server.

## Development

```bash
uv sync
uv run pytest                  # full suite
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

CI runs all of the above on every push and PR, plus a dedicated **`Equivalence gate (rebuild ≡ incremental)`** step — a hypothesis property test proving that dropping the projection and replaying the log always reproduces the incremental state byte-for-byte. That gate is the M0 exit criterion and is kept forever (TDD §5); it extends to server projections at M1 and the browser cache at M2. At M1 the `pytest` step also runs the `integration`-marked E2Es — including the exit-gate test that stands up a real Postgres + the live server and drives two `msgctl` clients to convergence — and a separate **`Simulation suite`** step runs the §12 property-based harness (four of six invariants at M1).

Contribution conventions: work is tracked in Linear (`ENG-xx`); commits follow `<type>[ENG-xx]: description`; protocol changes require amending `docs/technical-design.md`, not drive-by PRs — the D1–D14 decision table is the contract.
