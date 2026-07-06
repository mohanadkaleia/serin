# ENG-75 — M2: web/ scaffolding (Vite + Vue 3 + TS + Pinia + Tailwind, single-origin via FastAPI, CI)

**Milestone:** M2 — Web client (first ticket; opens the milestone).
**Tech-lead:** planning complete; implementation delegated per assignments below.
**TDD refs:** §1.1 (repo layout — `web/` tree), §5.1 (web architecture, D4), §5.2 (Dexie schema — anticipated layout), §5.3 (sync engine, worker — anticipated), §10/§11 (single-origin: FastAPI serves the SPA; multi-stage image bakes `web/dist`), §12 (Playwright golden-path — deferred to ENG-83). Mirrors the M0/M1 scaffold discipline of `.claude/chat/eng-63-server-scaffold.md` (build the container, not the behaviour).

## Goal (restated)

Stand up the `web/` **client skeleton** that every later M2 ticket lands into (ENG-76 TS core/crypto, ENG-77 SharedWorker, ENG-82 Pinia stores over worker RPC, ENG-83 Playwright golden-path). This ticket builds the **toolchain and single-origin serving contract**, not chat behaviour: a Vite + Vue 3 + TypeScript-**strict** + Pinia + Tailwind + Vue Router project; a strict tsconfig matched to the Python mypy-strict bar; ESLint (flat) + Prettier CI-enforced like ruff; Vitest (jsdom) with one smoke; a Playwright harness stub (config only, no golden path); a **single-origin serve mechanism** (FastAPI `StaticFiles` mount serving `web/dist` with an SPA fallback that must NOT shadow `/v1/*`, `/healthz`, `/metrics`; Vite dev-server proxies `/v1` + `/v1/ws` to uvicorn); a trivial `App.vue` + `HelloWorld` + Pinia store stub + one Vitest smoke so build/test/typecheck/lint have real (tiny) content; and a new parallel `web` CI job.

Areas touched: new `web/` tree (all new), one **append-only** addition to `server/msgd/api/app.py`, one new field-block in `server/msgd/settings.py`, one new `web` job in `.github/workflows/ci.yml`. No `server/` behaviour changes, no `cli/`, no Dexie/worker/crypto code (those are ENG-76/77/82).

---

## Decisions pinned

### D-1 · Package manager — **pnpm**, pinned via Corepack; Node **22 LTS** pinned

**Rule: pnpm.** It is the JS-side analog of the server's `uv` discipline: a content-addressed store (fast, disk-cheap) and a **strict, non-flat `node_modules`** that forbids phantom dependencies — the same "no undeclared imports" rigor as mypy-strict/ruff on the Python side. npm's zero-install friction is real but its flat `node_modules` lets undeclared transitive deps import successfully and rot silently; we reject that for the same reason we run strict typing. CI caching is a wash (both cache well via `actions/setup-node`'s `cache:` input), so it does not tip the decision.

- **Pin pnpm via Corepack**, not a global install: add `"packageManager": "pnpm@9.15.0"` to `web/package.json`. Corepack (bundled with Node) then guarantees byte-identical pnpm across dev + CI. No `npm i -g pnpm` step.
- **Commit `web/pnpm-lock.yaml`.** CI installs with `pnpm install --frozen-lockfile` (the `uv sync --locked` analog — fails on a stale lock).
- **Node pinned two ways:** `web/.nvmrc` → `22` and `package.json` `"engines": { "node": ">=22 <23" }`. Rule **Node 22 (LTS "Jod")**, not the newer current line, for CI reproducibility and a supported LTS through the M2–M6 horizon. (Local dev may run newer; CI is the source of truth and pins 22 via `setup-node`.)
- **Scope:** everything lives under `web/`. This is a **separate JS project**, deliberately not a uv/Python workspace member — it has its own lockfile, its own toolchain, its own CI job. The only coupling to the server is the runtime single-origin serve (D-4) and the eventual image bake (noted, deferred).

### D-2 · `web/` layout (§1.1) — create only what the smoke needs; seed the M2-shaped dirs

Per §1.1 the tree is `src/{worker,stores,components}/`. This ticket adds `src/core/` (ENG-76 TS envelope/JCS/hashing lands here), `src/router/`, and `src/views/`. **No empty dirs** beyond what the smoke populates: `worker/` is created **only** with a placeholder note in the ticket, not an empty folder (Vite's worker build target is *configured* now — D-3 — but no worker file ships until ENG-77). `stores/` gets one real stub store; `components/` one real component; `core/` gets nothing this ticket (documented seam for ENG-76).

```text
web/
  .nvmrc                     # 22
  package.json               # scripts + engines + packageManager (pnpm@…)
  pnpm-lock.yaml             # committed, --frozen-lockfile in CI
  tsconfig.json              # strict base (D-6), references app + node configs
  tsconfig.app.json          # app compilation (DOM + WebWorker libs, src/**)
  tsconfig.node.json         # vite.config / tooling (Node types)
  vite.config.ts             # Vue plugin, dev proxy (D-4), worker build target (D-3), vitest inline config
  vitest.config.ts           # OR merged into vite.config via `test:` — rule: single vite.config with `test` block (D-3)
  tailwind.config.ts         # content globs → ./index.html + ./src/**/*.{vue,ts}
  postcss.config.js          # tailwindcss + autoprefixer
  eslint.config.ts           # flat config (D-6): @typescript-eslint + eslint-plugin-vue + prettier-off
  .prettierrc.json           # prettier config (source of formatting truth)
  playwright.config.ts       # harness stub only (D-3) — no golden-path spec
  index.html                 # Vite entry; <div id="app">, module script src/main.ts
  env.d.ts                   # vite/client + vue shim types
  src/
    main.ts                  # createApp(App).use(createPinia()).use(router).mount('#app')
    App.vue                  # trivial shell: <RouterView/> + <HelloWorld/>
    router/
      index.ts               # createRouter(createWebHistory()), one '/' route → HomeView
    views/
      HomeView.vue           # renders HelloWorld + reads the stub store
    components/
      HelloWorld.vue         # trivial component (props: msg) — the typecheck/test subject
    stores/
      counter.ts             # Pinia store stub (defineStore) — real (tiny) content
    style.css                # @tailwind base/components/utilities
  tests/
    unit/
      HelloWorld.spec.ts     # Vitest + @vue/test-utils smoke: mounts, asserts prop render
      counter.spec.ts        # Vitest: Pinia store stub increments (setActivePinia)
    e2e/
      .gitkeep               # Playwright harness target dir; real spec is ENG-83
  # NOTE (documented seam, NOT created this ticket):
  #   src/core/     → ENG-76 (TS envelope / JCS / hashing; must pass core/testdata vectors)
  #   src/worker/   → ENG-77 (SharedWorker: socket, Dexie, sync engine, outbox)
  #   src/stores/*  → ENG-82 (Pinia stores fed by worker postMessage RPC)
```

`core/testdata/vectors.json` (§1.1/§12: "Python and TS must both pass the same vectors") is consumed by ENG-76's tests, not this ticket — noted so ENG-76 wires Vitest at it.

### D-3 · Toolchain + exact scripts

**`package.json` scripts (rule these exact names — CI and humans depend on them):**

```jsonc
"scripts": {
  "dev":       "vite",
  "build":     "vue-tsc --noEmit && vite build",   // typecheck gates the build
  "preview":   "vite preview",
  "test":      "vitest run",                         // CI (non-watch)
  "test:watch":"vitest",
  "typecheck": "vue-tsc --noEmit -p tsconfig.app.json",
  "lint":      "eslint .",
  "lint:fix":  "eslint . --fix",
  "format":    "prettier --write .",
  "format:check":"prettier --check .",
  "e2e":       "playwright test"                     // harness stub; no specs pass/fail yet → --passWithNoTests-equivalent
}
```

- **Vitest**, `environment: 'jsdom'` (chosen now because ENG-77 Dexie/worker tests need a DOM/IndexedDB-ish env; `fake-indexeddb` will be added by those tickets). Config lives **inline in `vite.config.ts`** under a `test:` block (single source of build+test config; avoids config drift) using `/// <reference types="vitest/config" />`. `@vue/test-utils` for component mounting. `jsdom` as a devDep.
- **ESLint flat config** (`eslint.config.ts`): `@typescript-eslint` (typed-lint, `parserOptions.project`), `eslint-plugin-vue` (`vue3-recommended`), and `eslint-config-prettier` last to disable stylistic rules that Prettier owns. **Prettier is the formatter; ESLint is the linter** — no `eslint-plugin-prettier` (keeps lint fast; formatting is checked separately via `format:check`). ESLint is **CI-enforced and blocking**, exactly like ruff (D-6).
- **`vue-tsc --noEmit`** is the typecheck gate (Vue SFC-aware `tsc`); it runs both standalone (`typecheck`) and as the first half of `build` so a type error fails the build like it fails CI.
- **Playwright: harness stub only.** `playwright.config.ts` present (baseURL, one chromium project, `webServer` commented/stubbed), `tests/e2e/` seeded with `.gitkeep`, **no golden-path spec** — that is ENG-83 (§12 login→send→reload→second-browser-live). `e2e` script exists but has nothing to run yet; do **not** wire it into the CI `web` job this ticket (ENG-83 adds the CI e2e step with a real server). Browsers are **not** installed in CI now.
- **Vite worker build target configured now** (§5.1 SharedWorker is ENG-77): set `worker: { format: 'es' }` in `vite.config.ts` and include `"WebWorker"` in `tsconfig.app.json` `lib` so a `new SharedWorker(new URL('./worker/…', import.meta.url), { type: 'module' })` compiles and bundles when ENG-77 lands. No worker file ships now — the config is ready, unused.

### D-4 · Single-origin serving (§5.1 D4, §10/§11) — THE load-bearing decision

**Contract:** the built SPA is served by the FastAPI app at `/` — one origin, no CORS, cookie/session handling stays simple (D4). In **dev**, Vite's dev server hosts the SPA on its own port and **proxies** `/v1` + `/v1/ws` to uvicorn, so the browser still sees one origin from the app's perspective and no CORS config is needed on the server.

**Mechanism (prod / built): `StaticFiles` mount at `/`, registered LAST, with a reserved-prefix-guarded SPA fallback.**

- Add a small `SPAStaticFiles(StaticFiles)` subclass (in a new `server/msgd/api/spa.py`, ~25 lines) that serves files from `web/dist` with `html=True`, and on a 404 for a would-be client-side route returns `index.html` — **except** it re-raises the 404 (does NOT serve index.html) when the path starts with a **reserved API prefix**. This keeps deep links like `/channel/abc` (Vue Router history mode) working while ensuring an unknown `/v1/whatever` still 404s as JSON, not as a masked HTML page.
- **Route precedence ruling (critical):** a Starlette `Mount` at `/` matches *every* path, so it MUST be added **after** every API router in `create_app()`. Starlette matches routes in registration order; because `health`, `auth`, `admin`, `events_upload`, `events_read`, `sync`, and `ws` routers are all `include_router`'d first, their specific paths win, and only genuinely unmatched paths fall through to the mount. Belt-and-suspenders: the reserved-prefix guard inside `SPAStaticFiles` refuses to emit `index.html` for `/v1`, `/healthz`, `/metrics` even if such a path reaches the mount. Both mechanisms together make it impossible for the SPA to shadow the API.
  - Reserved prefixes to guard: `("v1", "healthz", "metrics")` (compared against the mount-relative path; `/v1/ws` is under `v1`). Documented as the single source; if a future top-level API route is added, this tuple is where it registers.
- **Guarded, append-only wiring in `app.py`:** at the very end of `create_app()`, after all `include_router` calls:
  ```python
  # ENG-75: single-origin SPA (§5.1 D4). Mounted LAST so API routes win;
  # SPAStaticFiles refuses index.html for reserved API prefixes (belt-and-suspenders).
  if settings.serve_spa and settings.web_dist_dir.is_dir():
      app.mount("/", SPAStaticFiles(directory=settings.web_dist_dir, html=True), name="spa")
  ```
  The `is_dir()` guard means: in **dev** (no `web/dist`) the mount is simply absent and Vite serves the SPA; in the **image** (dist baked in) the mount activates. No behaviour change to any existing test (they hit `/v1`/`/healthz` and never build a dist). Total server diff: ~30 lines (`spa.py`) + ~4 lines (`app.py`) + 2 settings fields — small and append-only, mirroring the ENG-63 discipline.
- **Settings additions** (`server/msgd/settings.py`, new block):
  ```python
  # --- Single-origin SPA (ENG-75, §5.1 D4) --------------------------------
  serve_spa: bool = True                              # image: on; can disable for API-only deploys
  web_dist_dir: Path = Path("web/dist")              # baked at /app/web/dist in the image
  ```
  Default `serve_spa=True` is safe because of the `is_dir()` guard — absent dist ⇒ no mount.
- **Vite dev proxy** (`vite.config.ts`):
  ```ts
  server: {
    proxy: {
      '/v1/ws': { target: 'ws://localhost:8080', ws: true },
      '/v1':    { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
  ```
  Order matters here too: the `ws` rule is declared before the plain `/v1` rule so websocket upgrades route correctly. Dev backend assumed at `:8080` (the uvicorn/entrypoint port).

**Image bake — noted, deferred.** §11 says the image is "multi-stage build: web dist baked into the Python image". This ticket does **not** modify the `Dockerfile` — adding a Node build stage that runs `pnpm build` and `COPY --from` the `web/dist` into `/app/web/dist` is a **follow-up devops ticket** (call it out to whoever owns the M2 image step). Rationale: the scaffolding ticket proves single-origin serving locally/in-CI without lengthening the image build or the `image` CI job; the bake is a clean separate change once the SPA has real content worth shipping. The `settings.web_dist_dir` default (`web/dist`, i.e. `/app/web/dist` under the image WORKDIR) is chosen now so the future `COPY` target is already fixed.

### D-5 · CI — a NEW parallel `web` job (devops-engineer)

**Rule: a new `web` job**, sibling to `checks` and `image`, not a step inside `checks`. The `checks` job is a uv/Python world (`setup-uv`, `uv sync`); the web job is a Node/pnpm world (`setup-node`, `pnpm install`). Mixing them would bloat both toolchains into one job for no isolation benefit and would serialize two independent ecosystems. Parallel jobs keep each fast and give distinct red/green signals ("python red" vs "web red"), exactly as `image` is already split out.

```yaml
  web:
    name: web · lint · type · test · build
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
      - name: Enable Corepack           # activates the pinned pnpm from package.json
        run: corepack enable
      - name: Set up Node 22
        uses: actions/setup-node@<SHA-PIN> # v4.x — devops resolves the digest per convention
        with:
          node-version: '22'
          cache: 'pnpm'
          cache-dependency-path: web/pnpm-lock.yaml
      - name: Install (frozen)
        working-directory: web
        run: pnpm install --frozen-lockfile
      - name: Lint
        working-directory: web
        run: pnpm lint
      - name: Format check
        working-directory: web
        run: pnpm format:check
      - name: Typecheck
        working-directory: web
        run: pnpm typecheck
      - name: Unit tests
        working-directory: web
        run: pnpm test
      - name: Build
        working-directory: web
        run: pnpm build           # produces web/dist (servable bundle)
```

- **SHA-pin `actions/setup-node`** by commit digest per the repo convention (every existing action in `ci.yml` is `@<sha> # vX.Y.Z`). devops resolves the current v4 digest at implementation time and pins it with the version comment. `actions/checkout` reuses the digest already in the file.
- **Corepack** activates pnpm before `setup-node`'s `cache: 'pnpm'` resolves (setup-node needs pnpm on PATH to compute the cache key) — order: `corepack enable` **then** `setup-node`.
- **No Playwright/e2e step** in this job (D-3) — no browsers installed, no server spun up; ENG-83 adds that.
- Fast by construction: install (cached) → lint → format → typecheck → test → build, all on a tiny codebase. Target wall-time well under the `checks` job.

### D-6 · TS strictness matched to the Python mypy-strict bar; ESLint CI-enforced like ruff

**`tsconfig.app.json` `compilerOptions` — rule these (the `mypy strict` analog):**

```jsonc
{
  "strict": true,                            // all strict-family flags
  "noUncheckedIndexedAccess": true,          // arr[i] is T | undefined — the big one
  "exactOptionalPropertyTypes": true,        // {a?: T} ≠ {a: T | undefined}
  "noImplicitOverride": true,
  "noFallthroughCasesInSwitch": true,
  "noUnusedLocals": true,
  "noUnusedParameters": true,
  "noImplicitReturns": true,
  "verbatimModuleSyntax": true,              // explicit `import type` — matters for the worker build
  "isolatedModules": true,                   // Vite/esbuild transpiles per-file
  "moduleResolution": "bundler",
  "module": "ESNext",
  "target": "ES2022",
  "lib": ["ES2022", "DOM", "DOM.Iterable", "WebWorker"],   // WebWorker for ENG-77 SharedWorker
  "types": ["vite/client"],
  "skipLibCheck": true                       // pragmatic: don't typecheck node_modules .d.ts
}
```

- Split configs: `tsconfig.json` (solution file with `references` to app + node), `tsconfig.app.json` (above, `include: src`, tests), `tsconfig.node.json` (`vite.config.ts`/tooling, `types: ["node"]`). Standard Vue-TS layout; keeps DOM libs out of the Node-tooling scope and vice-versa.
- **ESLint is blocking in CI** (D-5 `pnpm lint` step) — the ruff analog. Typed linting (`@typescript-eslint` with `projectService`/`parserOptions.project`) so rules that need type info fire. `eslint-plugin-vue` `vue3-recommended`. `eslint-config-prettier` last so lint and format don't fight.
- **Prettier** owns formatting; `format:check` is a separate blocking CI step (the `ruff format --check` analog).

### D-7 · Real (tiny) content so build/test/typecheck/lint are meaningful

Per ticket item 7: a trivial `App.vue`, a `HelloWorld` component (typed props), one Pinia store stub (`counter.ts`), and Vitest smokes. These give every gate real work: `HelloWorld.vue` exercises SFC typechecking + `noUncheckedIndexedAccess`; `HelloWorld.spec.ts` mounts it and asserts prop render (proves Vitest+jsdom+@vue/test-utils wired); `counter.spec.ts` proves Pinia testing (`setActivePinia(createPinia())`); `vite build` proves a servable bundle emits to `web/dist`. Kept deliberately minimal — this is scaffolding, not UI.

---

## Implementation plan (step-by-step)

### Part A — `web/` project + configs + smoke (ui-engineer)

1. **Bootstrap `web/`** with pnpm: `package.json` (name `@msg/web`, `private: true`, `packageManager: "pnpm@9.15.0"`, `engines.node`, the D-3 scripts), `.nvmrc` (`22`). Add deps: runtime `vue`, `vue-router`, `pinia`; dev `vite`, `@vitejs/plugin-vue`, `typescript`, `vue-tsc`, `vitest`, `jsdom`, `@vue/test-utils`, `eslint`, `@eslint/js`, `typescript-eslint`, `eslint-plugin-vue`, `eslint-config-prettier`, `prettier`, `tailwindcss`, `postcss`, `autoprefixer`, `@playwright/test`, `@types/node`. Generate + commit `pnpm-lock.yaml`.
2. **TS configs** (D-6): `tsconfig.json` (solution + references), `tsconfig.app.json` (strict block), `tsconfig.node.json` (Node tooling). `env.d.ts` with `vite/client` + Vue SFC shim.
3. **Vite config** (`vite.config.ts`): `@vitejs/plugin-vue`, dev `server.proxy` for `/v1/ws` + `/v1` (D-4), `worker: { format: 'es' }` (D-3), inline `test:` block (`environment: 'jsdom'`, `globals: true`). `/// <reference types="vitest/config" />`.
4. **Tailwind/PostCSS**: `tailwind.config.ts` (content globs), `postcss.config.js`, `src/style.css` with the three `@tailwind` directives.
5. **ESLint + Prettier** (D-6): `eslint.config.ts` flat (js recommended + typescript-eslint + vue3-recommended + prettier-off), `.prettierrc.json`.
6. **App source**: `index.html`, `src/main.ts` (Vue + Pinia + Router), `src/App.vue` (`<RouterView/>`), `src/router/index.ts` (one `/` route → `HomeView`), `src/views/HomeView.vue`, `src/components/HelloWorld.vue` (typed `msg` prop), `src/stores/counter.ts` (Pinia stub).
7. **Tests**: `tests/unit/HelloWorld.spec.ts` (mount + assert), `tests/unit/counter.spec.ts` (Pinia store), `tests/e2e/.gitkeep`, `playwright.config.ts` (stub — baseURL, chromium project, webServer commented).
8. **Verify locally**: `pnpm install && pnpm lint && pnpm format:check && pnpm typecheck && pnpm test && pnpm build` all green; `web/dist/index.html` + hashed assets emitted.

### Part B — single-origin server wiring (ui-engineer; append-only server edit)

9. **`server/msgd/settings.py`**: add the `serve_spa: bool = True` + `web_dist_dir: Path = Path("web/dist")` block (D-4).
10. **`server/msgd/api/spa.py`** (new, ~25 lines): `SPAStaticFiles(StaticFiles)` with the reserved-prefix-guarded index.html fallback (D-4). Reserved prefixes `("v1", "healthz", "metrics")`.
11. **`server/msgd/api/app.py`**: append the guarded `app.mount("/", SPAStaticFiles(...))` after the last `include_router`, with the D-4 comment. Import `SPAStaticFiles`. Nothing else changes.
12. **Serve test** (`server/tests/test_spa.py`, new): build a tiny fixture `dist/` (a temp dir with `index.html`), point `settings.web_dist_dir` at it via override, and assert with the ASGI client: (a) `GET /` → 200 and returns the fixture `index.html`; (b) `GET /some/client/route` → 200 index.html (SPA fallback); (c) `GET /v1/does-not-exist` → 404 **not** index.html (API not shadowed); (d) `GET /healthz` still 200 JSON (existing route wins). This is the automated "single-origin works and `/v1` isn't shadowed" gate. Marked non-integration (no DB needed beyond the existing app fixture; if the app fixture requires DB, reuse the harness but the assertions are route-level).

### Part C — CI `web` job (devops-engineer)

13. **`.github/workflows/ci.yml`**: add the `web` job (D-5) parallel to `checks`/`image`. SHA-pin `actions/setup-node` (resolve current v4 digest), reuse the `actions/checkout` digest. Corepack-enable before setup-node; `cache: 'pnpm'` + `cache-dependency-path: web/pnpm-lock.yaml`; steps install→lint→format:check→typecheck→test→build, all `working-directory: web`. No e2e step (deferred to ENG-83).

---

## Test plan

| Test | Asserts | Owner |
|---|---|---|
| `pnpm build` | `vue-tsc --noEmit` passes then `vite build` emits a servable `web/dist` (index.html + hashed JS/CSS) | ui |
| `pnpm typecheck` | strict tsconfig (incl. `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`) is clean on real source | ui |
| `pnpm lint` + `format:check` | ESLint (flat, typed, vue3) + Prettier green — the ruff analog, blocking | ui |
| `HelloWorld.spec.ts` | Vitest+jsdom+@vue/test-utils mount, prop renders | ui |
| `counter.spec.ts` | Pinia store stub increments under `setActivePinia` | ui |
| `test_spa.py` (a) `GET /` | serves `index.html` from `web_dist_dir` | ui |
| `test_spa.py` (b) unknown client route | SPA fallback → 200 index.html (Vue Router history mode works) | ui |
| `test_spa.py` (c) `GET /v1/nope` | **404, not index.html** — API not shadowed (precedence + reserved-prefix guard) | ui |
| `test_spa.py` (d) `GET /healthz` | existing route still wins (200 JSON) | ui |
| existing `server/tests/*` | unchanged/green — no dist present ⇒ mount absent, zero behaviour change | ui |
| CI `web` job | install→lint→format→typecheck→test→build all green in parallel with `checks`/`image` | devops |

**Manual/documented check** (in lieu of a full e2e this ticket): `pnpm build` in `web/`, run the server with `MSG_WEB_DIST_DIR=$(pwd)/web/dist` (or default), `curl localhost:8080/` → SPA HTML, `curl localhost:8080/v1/anything` → 404 JSON, `curl localhost:8080/healthz` → `{"status":"ok"}`. The real golden-path (login→send→reload→second-browser-live) is ENG-83.

---

## Risks & open questions

- **SPA mount shadowing the API** — the headline risk. Mitigated two ways (D-4): mount registered **last** (Starlette order → specific API routes win) **and** the reserved-prefix guard inside `SPAStaticFiles` (refuses index.html for `v1`/`healthz`/`metrics`). `test_spa.py` (c)/(d) pin both. Any new top-level API route must be added to the reserved-prefix tuple — documented at the tuple.
- **`exactOptionalPropertyTypes` friction with Vue/vue-router types.** Some `@types` in the ecosystem aren't written to this flag and can surface false-ish errors at boundaries. Mitigation: `skipLibCheck: true` (already ruled) confines strictness to our source; if a specific third-party boundary genuinely can't satisfy it, narrow at the call site rather than dropping the flag globally. Keep the flag — it's part of the mypy-strict-parity bar.
- **Corepack + `cache: 'pnpm'` ordering in CI.** `setup-node`'s pnpm cache needs pnpm resolvable when it runs; hence `corepack enable` **before** `setup-node`. If a runner ships a Corepack that prompts for the pinned pnpm signature, add `COREPACK_ENABLE_DOWNLOAD_PROMPT=0` to the job env. Watch first CI run.
- **Node 22 vs local Node 25.** Dev machines may run newer Node; CI pins 22 (LTS) as source of truth. `engines` is advisory (`pnpm` warns, doesn't hard-fail by default) — acceptable; CI enforces the real version.
- **jsdom vs the future worker/Dexie tests.** jsdom has no real IndexedDB; ENG-77 will add `fake-indexeddb` and possibly a `@vitest/web-worker`-style shim. Choosing jsdom now (not `happy-dom`) keeps that path open; noted for ENG-77.
- **Image bake deferred.** `web/dist` is NOT copied into the image this ticket (D-4). Until the follow-up devops ticket adds the Node build stage + `COPY --from … /app/web/dist`, the image serves API-only (mount absent, `serve_spa` guarded by `is_dir()`). Flag to the M2 image owner so single-origin actually ships in the container. Not a blocker for this scaffolding ticket.
- **Prettier vs ESLint double-report.** Avoided by using `eslint-config-prettier` (disables stylistic ESLint rules) and running Prettier as a *separate* check, not via `eslint-plugin-prettier`. Keeps lint fast and reports unambiguous.
- **Open:** does the existing app test fixture (`create_app`) require a live DB just to test the SPA routes? If so, `test_spa.py` reuses the migrated-DB harness (marked integration); if the app can be constructed without a DB connection for pure route assertions, keep it Docker-free. Resolve during implementation — not a blocker; the assertions are route-level regardless.

---

## Agent assignments

- **ui-engineer** — Part A (entire `web/` project: configs, source, smoke tests) **and** Part B (the append-only `settings.py` + `spa.py` + `app.py` single-origin wiring and `test_spa.py`). Owns the pnpm/Node/tsconfig/eslint/vite decisions as implemented per D-1/2/3/4/6/7.
- **devops-engineer** — Part C (the new `web` CI job): Node/pnpm setup, Corepack, SHA-pinned `actions/setup-node`, caching, the install→lint→format→typecheck→test→build pipeline. Light-reviews Part B's server diff for the append-only/precedence property.
