// tests/unit/worker/secret-store.spec.ts — the SecretStore seam (ENG-168,
// M6-4): the session token's at-rest home, factored out of the MsgDb `meta`
// table so the desktop can keep it OUT of the portable workspace folder.
//
// Coverage:
//   • the two adapters' KV contract (Memory / Meta-over-MsgDb);
//   • WEB UNCHANGED: an AuthManager with the DEFAULT store persists/restores/
//     clears the token through the SAME Dexie/Memory `meta` rows as before;
//   • DESKTOP: an injected store carries the token; the `meta` table never
//     sees it; restore/logout ride the store;
//   • the fail-closed guard: `SqliteDb.metaPut(META_SESSION_TOKEN, …)` throws,
//     so a mis-wired desktop core errs loudly instead of leaking the token
//     into `projections.sqlite3`.

import { describe, expect, it } from 'vitest'

import { AuthManager } from '../../../src/worker/auth'
import { MemoryDb, openDb } from '../../../src/worker/db'
import { createHttpClient, type HttpClient } from '../../../src/worker/http'
import {
  MemorySecretStore,
  MetaSecretStore,
  type SecretStore,
} from '../../../src/worker/secret-store'
import { openSqliteDb } from '../../../src/worker/sqlite/sqlite-db'
import {
  META_DEVICE_ID,
  META_MY_USER_ID,
  META_SESSION_TOKEN,
  type MsgDb,
} from '../../../src/worker/types'

import { fakeIdbOptions } from './helpers'

const TOKEN = 'sess-tok-SECRET-m6-4-do-not-leak'

function loginFetch(): typeof fetch {
  return () =>
    Promise.resolve(
      new Response(
        JSON.stringify({
          token: TOKEN,
          user_id: 'u_1',
          device_id: 'd_1',
          workspace_id: 'w_1',
          role: 'member',
          expires_at: '2099-01-01T00:00:00Z',
        }),
        { status: 200 },
      ),
    )
}

function makeManager(db: MsgDb, secrets?: SecretStore): { manager: AuthManager; http: HttpClient } {
  const holder: { manager: AuthManager | null } = { manager: null }
  const http = createHttpClient({
    fetchImpl: loginFetch(),
    getToken: () => holder.manager?.getToken() ?? null,
    onUnauthorized: () => holder.manager?.clearSession(),
  })
  const manager = new AuthManager(db, http, secrets)
  holder.manager = manager
  return { manager, http }
}

describe('SecretStore adapters — the KV contract', () => {
  it('MemorySecretStore round-trips set/get/delete; absent is null', async () => {
    const store = new MemorySecretStore()
    expect(await store.get('k')).toBeNull()
    await store.set('k', 'v-1')
    expect(await store.get('k')).toBe('v-1')
    await store.set('k', 'v-2')
    expect(await store.get('k')).toBe('v-2')
    await store.delete('k')
    expect(await store.get('k')).toBeNull()
  })

  it('MetaSecretStore delegates to the SAME meta rows metaGet reads (web parity)', async () => {
    const db = new MemoryDb()
    const store = new MetaSecretStore(db)
    expect(await store.get(META_SESSION_TOKEN)).toBeNull()
    await store.set(META_SESSION_TOKEN, TOKEN)
    // The value is readable through plain metaGet — the exact pre-M6-4 storage.
    expect(await db.metaGet(META_SESSION_TOKEN)).toBe(TOKEN)
    expect(await store.get(META_SESSION_TOKEN)).toBe(TOKEN)
    await store.delete(META_SESSION_TOKEN)
    // The clear is the pre-M6-4 `metaPut(key, undefined)` shape, byte-identical.
    expect(await db.metaGet(META_SESSION_TOKEN)).toBeUndefined()
    expect(await store.get(META_SESSION_TOKEN)).toBeNull()
  })
})

describe('AuthManager × SecretStore — token persistence routing (ENG-168)', () => {
  it('WEB (default store): the token stays in the Dexie meta rows, unchanged', async () => {
    const db = await openDb(fakeIdbOptions())
    const { manager } = makeManager(db) // no injected store → MetaSecretStore
    const res = await manager.login({ email: 'a@b.co', password: 'password1234' })
    expect(res.ok).toBe(true)
    // Exactly where it lived before M6-4 — same table, same key.
    expect(await db.metaGet(META_SESSION_TOKEN)).toBe(TOKEN)

    // A fresh manager restores from those rows (reload persistence, R6)…
    const { manager: fresh } = makeManager(db)
    await fresh.restore()
    expect(fresh.status().authenticated).toBe(true)
    expect(fresh.getToken()).toBe(TOKEN)

    // …and logout clears them (device_id survives, as before).
    await fresh.logout()
    expect(await db.metaGet(META_SESSION_TOKEN)).toBeUndefined()
    expect(await db.metaGet(META_DEVICE_ID)).toBe('d_1')
    await db.close()
  })

  it('DESKTOP (injected store): the token lives in the store, NEVER in meta', async () => {
    const db = new MemoryDb()
    const secrets = new MemorySecretStore()
    const { manager } = makeManager(db, secrets)
    const res = await manager.login({ email: 'a@b.co', password: 'password1234' })
    expect(res.ok).toBe(true)

    expect(await secrets.get(META_SESSION_TOKEN)).toBe(TOKEN)
    expect(await db.metaGet(META_SESSION_TOKEN)).toBeUndefined() // meta never sees it
    // Non-secret identity still rides meta (outbox/mirror read it there).
    expect(await db.metaGet(META_DEVICE_ID)).toBe('d_1')
    expect(await db.metaGet(META_MY_USER_ID)).toBe('u_1')

    // Restore hydrates from the store; logout clears the store.
    const { manager: fresh } = makeManager(db, secrets)
    await fresh.restore()
    expect(fresh.status().authenticated).toBe(true)
    expect(fresh.getToken()).toBe(TOKEN)
    await fresh.logout()
    expect(await secrets.get(META_SESSION_TOKEN)).toBeNull()
    expect(fresh.getToken()).toBeNull()
  })
})

describe('SqliteDb.metaPut token guard (fail-closed, ENG-168)', () => {
  it('REFUSES META_SESSION_TOKEN and keeps every other meta key working', async () => {
    const db = await openSqliteDb(':memory:')
    await expect(db.metaPut(META_SESSION_TOKEN, 'tok')).rejects.toThrow(/SecretStore/)
    expect(await db.metaGet(META_SESSION_TOKEN)).toBeUndefined()
    // Other keys are unaffected — the guard is surgical.
    await db.metaPut(META_DEVICE_ID, 'd_1')
    await db.metaPut('projection_version', 6)
    expect(await db.metaGet(META_DEVICE_ID)).toBe('d_1')
    expect(await db.metaGet('projection_version')).toBe(6)
    expect(await db.count('meta')).toBe(2)
    await db.close()
  })

  it('a mis-wired desktop core (default MetaSecretStore over SqliteDb) fails LOUDLY on login', async () => {
    const db = await openSqliteDb(':memory:')
    const { manager } = makeManager(db) // no injected store — the mis-wiring
    await expect(manager.login({ email: 'a@b.co', password: 'password1234' })).rejects.toThrow(
      /SecretStore/,
    )
    // Nothing leaked into the sqlite meta table.
    expect(await db.metaGet(META_SESSION_TOKEN)).toBeUndefined()
    await db.close()
  })
})
