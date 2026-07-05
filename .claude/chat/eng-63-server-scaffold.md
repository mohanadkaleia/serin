# ENG-63 — M1: Server scaffolding (FastAPI, async SQLAlchemy, Alembic, full §4.2 schema, test harness)

**Milestone:** M1 — Sync server (first ticket; opens the milestone).
**Tech-lead:** planning complete; implementation delegated per assignments below.
**TDD refs:** §1.1 (repo layout), §4.1 (stack), §4.2 (schema — verbatim, lines 317–433), §4.3 (observability guardrails), §11 (deployment / migrations on startup), §12 (test strategy), D1/D2/D14 (hashing & ordering invariants).

## Goal (restated)

Stand up the `msgd` **server** skeleton that every later M1 ticket (ENG-64 auth, ENG-65 upload/sequencing, ENG-66 pull/sync, ENG-67 WS fanout, …) lands into. This ticket builds the container, not the behaviour: a FastAPI app factory, env-driven settings, structured JSON logs, `/healthz` with a real DB ping, the **complete §4.2 Postgres schema** as SQLAlchemy 2 typed async models, Alembic wired for the async engine with an initial migration that reproduces §4.2 exactly, migrations-run-on-startup mechanics, and a testcontainers + in-process-ASGI test harness wired into CI. No auth, no event accept path, no routers beyond health — those tables migrate now even though their APIs arrive later (ticket scope, explicit).

Areas touched: `server/msgd/` (new `settings.py`, `logging.py`, `db/`, `api/`), `server/tests/` (harness), root `pyproject.toml` + `server/pyproject.toml` (deps), `.github/workflows/ci.yml` (light step edit), `server/` entrypoint. No `cli/` code changes, no `web/`.

---

## Decisions pinned

### D-1 · Dependencies + the CLI edge

**Add to `server/pyproject.toml` runtime deps (`msgd`):**
`fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `pydantic-settings`.

**Defer `argon2-cffi` to ENG-64 (auth).** This ticket implements no password hashing. `users.password_hash` is a plain `TEXT` column — defining it needs no crypto lib. `argon2-cffi` is a *behaviour* dependency of the auth ticket; adding it here would be an unused import. Rule: **it lands with ENG-64.**

**Dev group (root `pyproject.toml` `[dependency-groups].dev`):** add `httpx`, `testcontainers[postgres]`, `pytest-asyncio`. (`httpx` drives the in-process ASGI client; `pytest-asyncio` runs the async fixtures/tests; testcontainers runs the ephemeral PG.) These never ship — dev-only.

**CLI edge — ACCEPT + DOCUMENT, defer extraction (lean).** `cli/msgctl` depends on `msgd` via a workspace source, so these heavy server deps (FastAPI, SQLAlchemy, asyncpg, Alembic) now flow into any `msgctl` install. This was **accepted at M0** (TDD §1.1 / §11: "`msgctl` ships inside the app image"), so it is operationally free today — the CLI runs inside the server image, which carries these deps regardless. Confirmed: `cli/msgctl/*` imports only `msgd.core.*` (envelope/jcs/hashing/ids/payloads) — none of the heavy deps are imported at CLI runtime; they are present-but-unused.

Rejected now, marked for later: extracting a third `msgd-core` workspace member (core + pydantic/ulid/rfc8785 only) so the CLI depends on core alone. That is the correct **M6 (desktop/Tauri)** move — the desktop build genuinely must not ship FastAPI — but doing it here is a large diff for zero present benefit. **Revisit marker: M6.** Document the edge in the plan and in a one-line comment on `msgd`'s dependency block.

### D-2 · Module layout

Create only what this ticket uses; anticipate ENG-64..69 by shape (seeded `routers/` package) but **no empty dirs** for `ws/`, `projections/`, `export/`, `plugins/` — those arrive with their tickets.

```text
server/
  alembic.ini                       # script_location -> msgd/db/migrations; url is a placeholder (env wins)
  docker-entrypoint.sh              # migrate to head, then exec uvicorn --workers 1 (one-worker comment)
  msgd/
    settings.py                     # Settings(BaseSettings), env_prefix="MSG_"; cached get_settings()
    logging.py                      # dictConfig JSON formatter; captures uvicorn access/error loggers
    api/
      __init__.py
      app.py                        # create_app() factory + lifespan (engine dispose, DB ping only)
      routers/
        __init__.py
        health.py                   # /healthz (DB ping) + /metrics stub
    db/
      __init__.py
      base.py                       # DeclarativeBase + MetaData(naming_convention=...)
      engine.py                     # create_async_engine, async_sessionmaker, get_session() dep
      models.py                     # ALL 11 §4.2 tables (single file for M1)
      migrate.py                    # run_migrations(url): programmatic alembic.command.upgrade(..., "head")
      migrations/
        env.py                      # async env: async_engine_from_config + run_sync(run_migrations)
        script.py.mako
        versions/
          0001_initial_schema.py    # == §4.2, hand-reviewed
  tests/
    conftest.py                     # session PG container, migrated-schema fixture, async session, ASGI client
    test_healthz.py
    test_migrations.py              # compare_metadata == empty; tsvector populates
```

`models.py` single-file for M1 (11 tables, readable). Split only if it grows past comfort in a later ticket. `alembic.ini` lives at `server/` root (standard) but startup/tests call **`msgd.db.migrate.run_migrations()`** programmatically (cwd-independent, works inside the image) rather than shelling `alembic`.

### D-3 · SQLAlchemy typed models, naming, types

- **SQLAlchemy 2.0 typed declarative** — `class Base(DeclarativeBase)`, `Mapped[...]` + `mapped_column(...)`. Required for the repo-wide **mypy `strict`** gate; Core `Table()` objects are not strict-friendly. No ORM relationships needed for M1 (query-layer tickets add them if useful).
- **Naming convention on `Base.metadata`** (load-bearing for Alembic autogenerate/`compare_metadata` determinism — without it, unnamed constraints get server-assigned names and every `alembic check` churns):
  ```python
  NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
  }
  ```
- **TEXT ids, no UUID types.** Every id/FK column is `mapped_column(Text)` → `Mapped[str]`. Ids are ULID strings from `msgd.core.ids` (client-mintable); the DB never generates them. Use `Text` explicitly (not `String(n)`) to match §4.2 verbatim.
- **TIMESTAMPTZ** → `DateTime(timezone=True)` → `Mapped[datetime]` (or `| None` for nullable: `deactivated_at`, `archived_at`, `edited_seq`-adjacent nullables). `DEFAULT now()` → `server_default=sa.text("now()")`.
- **BIGINT** → `BigInteger`/`Mapped[int]`; **INT** → `Integer`; **BOOLEAN** → `Boolean`/`Mapped[bool]`; **JSONB** → `postgresql.JSONB`/`Mapped[dict[str, Any]]`.
- **CHECK constraints** (`role IN (...)`, `kind IN (...)`, `visibility IN (...)`) as named `CheckConstraint(..., name="role_valid")` in `__table_args__` so the `ck` convention renders a stable name.
- **FKs EXACTLY as §4.2 declares `REFERENCES`** — and no more. Only these have FKs: `users→workspaces`, `devices→users`, `sessions→users`+`devices`, `streams→workspaces`, `stream_members→streams`+`users`, `events.stream_id→streams`. Everything else (`events.workspace_id`/`author_user_id`, all of `messages_proj`, `read_state`, `files`, `invites`) is bare `TEXT` per the schema. Adding FKs the schema omits would break `compare_metadata` and diverge from the contract.
- Composite keys: `events` PK `(stream_id, server_sequence)` + `UNIQUE (workspace_id, event_id)`; `stream_members` PK `(stream_id, user_id)`; `read_state` PK `(user_id, stream_id)`.
- **Indexes:** `messages_proj` GIN on `search_tsv`; `files (workspace_id, sha256)`.

### D-4 · `events.client_created_at` is derived metadata, NEVER a hash source (D1/D14) — stated loudly

`events.body` (JSONB) is stored **verbatim** and is the **sole** input to `event_hash` (D1: SHA-256 over RFC 8785 JCS of `body` only; the server never mutates `body`). `client_created_at` (TIMESTAMPTZ) is a **derived, queryable projection** of the timestamp string that also lives inside `body`: at accept time (ENG-65, not this ticket) it is parsed out of the body for SQL-level filtering/ordering support, and it is **untrusted for ordering** (D14 — ordering comes from `server_sequence`/server time only).

> **Nobody ever computes `event_hash` from the column.** Postgres TIMESTAMPTZ storage normalizes the textual form (offset/precision), so a column round-trip would not reproduce the client's verbatim string. The canonical string is the one inside `body`; the column is a lossy convenience copy. This scaffolding ticket only **defines** the column — it is populated by the accept path. Any engineer touching hashing reads `body`, never the column.

Comment this invariant directly in `models.py` above the `events` table so it survives past the plan.

### D-5 · Migrations on startup — entrypoint, NOT lifespan

**Rule: `alembic upgrade head` runs in a container entrypoint before uvicorn boots; app lifespan does DB-**ping**-only, never DDL.**

- **Prod/compose:** `docker-entrypoint.sh` → `python -m msgd.db.migrate` (programmatic upgrade-to-head) → `exec uvicorn msgd.api.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1`. Single, deterministic place; clean for `docker compose up`. The **one-worker constraint is documented in the entrypoint** (comment: in-process WS registry / fanout; horizontal scale needs shared pub/sub — out of MVP scope, per §11 & compose note).
- **Why not lifespan:** DDL-on-app-boot couples request serving to migrations, would re-run per worker (we have one, but the principle holds), and muddies tests (which own schema via fixtures). Lifespan here only: create engine, `SELECT 1` sanity ping, dispose engine on shutdown.
- **Tests/CI never migrate via app startup.** The harness applies schema once per session by calling the same `run_migrations()` helper against the ephemeral container (this doubles as the "migration from empty == §4.2" gate and proves the GENERATED-column DDL applies). App under test receives an already-migrated DB.

`msgd/db/migrate.py` builds an Alembic `Config` in code (script_location resolved relative to the package; URL from `Settings`/arg) and calls `alembic.command.upgrade(cfg, "head")` — no cwd dependence, works identically in the image and in tests. Note: `migrate.py` uses a **sync** engine for the upgrade (Alembic's command API is sync; env.py bridges async via `run_sync`), or async env with `async_engine_from_config` — env.py handles the async engine; `run_migrations()` may call the sync `command.upgrade` which our async `env.py` drives. Implement env.py in the standard async pattern (`connectable.connect()` → `connection.run_sync(do_run_migrations)`), `target_metadata = Base.metadata`, `compare_type=True`.

### D-6 · Test fixture architecture

- **Session-scoped Postgres container** (`testcontainers[postgres]`, `postgres:17` to match compose §11). One container per test session — the cost is paid once.
- **Schema applied once per session via `run_migrations()`** (real Alembic, not `metadata.create_all`). This is deliberate: it exercises the actual migration path and validates the tsvector GENERATED column + GIN index DDL for real.
- **Per-test isolation = transaction rollback** (fastest; no re-migrate, no truncate between tests). Standard SQLAlchemy "bind session to an outer transaction, roll back after each test" pattern, async variant: open a connection, `begin()`, bind the `AsyncSession`, yield, `rollback()`, close. App-level tests that hit real endpoints via the ASGI client override the app's `get_session` dependency (`app.dependency_overrides`) to yield the transaction-bound session so writes stay inside the rolled-back tx.
  - **Fallback:** a `truncate`-all fixture for tests that genuinely need committed, cross-connection visibility (the future WS/simulation suite may). Documented as the escape hatch; default stays rollback.
  - `/healthz` needs no isolation — it's a read-only ping against the migrated DB.
- **In-process ASGI** via `httpx.AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")`.
- **Config:** `asyncio_mode = "auto"` in pytest config so async tests/fixtures need no per-item marker.
- **CI budget:** container start + readiness (~few s) + `run_migrations` (<1 s) ≪ the 30 s acceptance bar. Watch: cold `postgres:17` image pull on a runner without a warm cache — mitigations in Risks. Target added CI wall-time: ≲ 60–90 s over current ~2 min.

### D-7 · CI wiring — same job, step edit only (so python-engineer owns it)

**Rule: one job, no new job.** testcontainers works on `ubuntu-latest` (Docker preinstalled). A separate job would re-pay checkout + `uv sync` + runner spin-up for no isolation benefit. The existing `Pytest` step (`uv run pytest`) already discovers `server/tests/` — once the harness deps are synced, server integration tests run there transparently (testcontainers spins PG inline). 

Minimal CI change: register a `@pytest.mark.integration` marker for the Docker-requiring tests so a **local** dev without Docker can `-m "not integration"`; **CI runs the full suite** (Docker present) with no marker filter. Optionally a `docker pull postgres:17` pre-step for predictable timing (devops call — see Risks). Because this is a **step-level edit, not a new job**, it stays with the primary implementer.

**Agent split:** **python-engineer** owns everything — models, settings, logging, app factory, health router, Alembic wiring + initial migration, entrypoint script, test harness, and the trivial CI/pyproject edits. **devops-engineer** is a **light reviewer only** (no separate implementation stream): sanity-check `docker-entrypoint.sh` (one-worker, exec form, migrate-before-serve) and the CI Docker assumption / optional pre-pull. If the entrypoint grows into a full `Dockerfile` + `docker-compose.yml` it is a **separate M1 ticket**; this ticket ships the entrypoint script + the one-worker documentation only.

### D-8 · `/metrics` — seed a zero-cost stub, defer the real thing

§4.3's real `/metrics` (Prometheus: event throughput, WS connection count, fanout latency) measures subsystems that don't exist at M1 scaffolding. **Rule: defer the real metrics to the observability/WS tickets; seed a trivial stub now** — `/metrics` returns a constant Prometheus-format text body (`content-type: text/plain; version=0.0.4`) with a `# TODO` comment. No `prometheus_client` dependency (a hand-written constant string). Keeps the ops surface discoverable and monitoring wireable at zero cost; real collectors land when there's something to collect.

### D-9 · The GENERATED tsvector column — how Alembic handles it

`messages_proj.search_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED` is the one place naive autogenerate is unreliable.

- Model it with **`mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))`** (`postgresql.TSVECTOR` type; `Computed(..., persisted=True)` → `GENERATED ALWAYS AS (...) STORED`; SQLAlchemy marks it non-insertable, so no code ever writes it). `Mapped[str]` (opaque) for typing.
- GIN index: `Index("ix_messages_proj_search_tsv", "search_tsv", postgresql_using="gin")`.
- **Initial migration is autogenerated then hand-corrected to match §4.2 verbatim.** Autogenerate is the draft; the initial migration is reviewed by hand for: the Computed column render, the GIN index, all `server_default=now()`, the CHECK constraints, the `events` composite PK + unique, and the `files`/`messages_proj` indexes. Commit as `0001_initial_schema`. The `compare_metadata` test (below) then locks models == migration forever.

---

## Implementation Plan (step-by-step, all python-engineer unless noted)

1. **Deps.** Edit `server/pyproject.toml`: add the six runtime deps (D-1) with a one-line comment on the CLI-edge acceptance + M6 revisit. Edit root `pyproject.toml`: add `httpx`, `testcontainers[postgres]`, `pytest-asyncio` to `dev`; add `asyncio_mode = "auto"` and a `markers = ["integration: needs Docker"]` block to `[tool.pytest.ini_options]`. `uv sync` → commit regenerated `uv.lock`.
2. **`msgd/settings.py`.** `Settings(BaseSettings)`, `SettingsConfigDict(env_prefix="MSG_")`. Fields: `database_url: str`, `data_dir: Path`, `secret_key: str`, `log_level: str = "INFO"`. `@lru_cache get_settings()`.
3. **`msgd/logging.py`.** `dictConfig`-based JSON formatter (~30 lines, no dep); route uvicorn `access`/`error` loggers through it. `configure_logging(level)` called by `create_app` and by `migrate.py`. Pass `log_config=None` to uvicorn in the entrypoint so our config wins.
4. **`msgd/db/base.py`.** `Base(DeclarativeBase)` with `metadata = MetaData(naming_convention=NAMING)`.
5. **`msgd/db/models.py`.** All 11 §4.2 tables per D-3/D-4/D-9. Verbatim column-for-column against lines 317–433. Add the D-4 hash-invariant comment above `events` and the D-9 Computed column on `messages_proj`.
6. **`msgd/db/engine.py`.** `create_async_engine(settings.database_url, pool_pre_ping=True)`, `async_sessionmaker(expire_on_commit=False)`, `async def get_session()` dependency. Engine created in lifespan, disposed on shutdown.
7. **`msgd/db/migrations/` (Alembic, async).** `alembic init`-style env.py rewritten async (`async_engine_from_config` / `run_sync(do_run_migrations)`), `target_metadata = Base.metadata`, `compare_type=True`, url from env/settings. `script.py.mako` default. `server/alembic.ini` with `script_location = msgd/db/migrations`.
8. **`msgd/db/migrate.py`.** `run_migrations(url: str | None = None)` → build `Config` in-code (script_location relative to package), `command.upgrade(cfg, "head")`. `python -m msgd.db.migrate` entry.
9. **Generate + hand-correct `versions/0001_initial_schema.py`** (D-9). Verify against §4.2 by eye + the tests in step 13.
10. **`msgd/api/routers/health.py`.** `GET /healthz` → engine `SELECT 1`, 200 `{"status":"ok"}` / 503 on failure. `GET /metrics` → stub constant text (D-8).
11. **`msgd/api/app.py`.** `create_app()` factory: configure logging, build settings, create engine, register lifespan (engine + startup ping, dispose on shutdown — **no DDL**), include health router. `--factory`-compatible.
12. **`server/docker-entrypoint.sh`** (devops light-review). `#!/usr/bin/env sh`, `set -e`, `python -m msgd.db.migrate`, `exec uvicorn msgd.api.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1 --log-config=...`. One-worker comment block (§11 rationale).
13. **`server/tests/conftest.py`** (D-6): session-scoped `postgres_container` fixture; session-scoped `migrated_db` (calls `run_migrations` against the container URL, sets `MSG_*` env / overrides settings); function-scoped transaction-rollback `db_session`; `client` (httpx ASGI, `get_session` overridden). Mark integration tests.
14. **`server/tests/test_healthz.py`** — `GET /healthz` → 200 `{"status":"ok"}` against the real migrated container.
15. **`server/tests/test_migrations.py`** — (a) after `run_migrations`, `compare_metadata(MigrationContext, Base.metadata)` is empty (models == migration == §4.2, no drift); (b) insert a `messages_proj` row, assert `search_tsv` is non-null (proves the GENERATED DDL applied).
16. **`.github/workflows/ci.yml`** (step edit): existing `uv run pytest` now runs server integration tests (Docker present on `ubuntu-latest`). Optional `docker pull postgres:17` pre-step for timing. No new job.

---

## Test plan

| Test | Asserts | Gate |
|---|---|---|
| `test_migrations::compare_metadata` | `alembic upgrade head` from empty == `Base.metadata` == §4.2, zero diff | model/migration drift prevention (permanent) |
| `test_migrations::tsvector` | inserted `messages_proj` row auto-populates `search_tsv` | GENERATED column DDL is real |
| `test_healthz` | `/healthz` 200 green against real Postgres | ticket acceptance #2 |
| harness timing | container + schema ready < 30 s in CI | ticket acceptance #3 |
| `uv run mypy` (existing) | typed models pass `strict` | repo gate |

---

## Risks & open questions

- **Autogenerate mishandles the tsvector GENERATED column / GIN expression.** Mitigation: model with `Computed(persisted=True)`, hand-review the initial migration, and the two `test_migrations` checks pin it. Budget time here — it's the fiddliest part.
- **`compare_metadata` false-positives** on `server_default` text (`now()` vs Postgres-normalized form), CHECK-constraint text normalization, or JSONB rendering can make the drift test churn. Mitigation: pin `server_default`/CHECK text to Postgres's normalized form; iterate against the container until the diff is genuinely empty. Known Alembic friction.
- **testcontainers cold image pull** could threaten the 30 s budget if `postgres:17` isn't cached on the runner. Mitigation: session-scoped (paid once), optional `docker pull` pre-step, or `postgres:17-alpine`. Watch first CI run's timing.
- **CLI-edge weight:** server deps now install with `msgctl`. Accepted/documented (D-1); the only real cost is a heavier CLI venv, operationally free while `msgctl` ships in the app image. **Revisit at M6** (desktop must not ship FastAPI) via `msgd-core` extraction.
- **Async transaction-rollback + FastAPI `dependency_overrides`** is subtle (SAVEPOINT semantics over asyncpg). M1 only `/healthz` needs it, but establish the pattern correctly now so ENG-65+ inherit a working harness; the `truncate` fallback covers cross-connection cases.
- **Migrations-on-startup vs tests:** app lifespan must never run DDL (tests own schema via fixtures). Enforced by keeping upgrade logic solely in `migrate.py`/entrypoint.
- **Required env at test time:** `MSG_DATABASE_URL` (from container), `MSG_SECRET_KEY`, `MSG_DATA_DIR` must be provided by the fixture/settings override or `pydantic-settings` will raise. Handle in `conftest`.
- **Open:** does `run_migrations()` use a sync or async engine internally? env.py is async (matches the app engine); `command.upgrade` is sync and drives env.py's async bridge. Confirm the asyncpg URL flows through env.py cleanly (it should via `async_engine_from_config`). Resolve during implementation; not a blocker.
