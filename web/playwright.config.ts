import { defineConfig, devices } from '@playwright/test'

// ENG-83 golden-path smoke + ENG-105 messaging-core golden path (TDD §12/§13):
// the ENG-83 spec is login → send → reload → history intact → a SECOND browser
// sees the message live (WS fanout); the ENG-105 spec drives the full M3 surface
// (react → edit → delete → thread reply → @mention → create channel → start DM)
// with a second browser seeing message + reaction + thread reply live. Both drive
// the REAL PRODUCTION topology: a real msgd server (Postgres testcontainer +
// subprocess uvicorn) serving the built SPA (web/dist), the /v1 API, and the
// /v1/ws WebSocket from ONE origin — no proxy, DEFAULT ws backend (ENG-92) —
// booted by global-setup (see serverctl.py). Heavier than the unit suite; its own
// CI job. The base URL is fixed by MSGD_E2E_PORT (default 8099).
const PORT = process.env.MSGD_E2E_PORT ?? '8099'

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  // The M3 messaging-core spec drives a long multi-context flow (two logins + many
  // WS round-trips), so allow generous headroom over the ENG-83 smoke, esp. in CI.
  timeout: 150_000,
  globalSetup: './tests/e2e/global-setup.ts',
  globalTeardown: './tests/e2e/global-teardown.ts',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
