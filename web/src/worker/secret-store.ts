// worker/secret-store.ts — the SecretStore seam (ENG-168, M6-4): where the
// session token lives AT REST, factored out of the MsgDb `meta` table so each
// platform can pick a storage with the right trust properties.
//
// Why this exists: on the desktop (M6), the projections DB
// (`projections.sqlite3`) sits INSIDE the portable workspace folder — the very
// folder a user hands to `msgctl verify`, zips into a bundle, or syncs across
// machines. A bearer token written into that folder travels with it. So the
// token's persistence is routed through this seam instead of `metaPut`:
//
//   • WEB — {@link MetaSecretStore}: delegates to the SAME Dexie `meta` rows
//     the token has always lived in (IndexedDB is origin-scoped browser
//     storage, not a portable folder), so web behavior/storage is UNCHANGED
//     byte-for-byte.
//   • DESKTOP — an injected platform store. The real OS-keychain
//     implementation ships with the Tauri shell (M6-5); tests and headless CI
//     use {@link MemorySecretStore}. As a second line of defense,
//     `SqliteDb.metaPut` REFUSES `META_SESSION_TOKEN` outright, so the token
//     cannot land in the workspace folder even through a mis-wired call.
//
// Only the SESSION TOKEN is a secret-at-rest here. `META_DEVICE_ID` stays in
// `meta` deliberately: it is a non-secret browser/install identity (cosmetic,
// shown in the sessions list, kept across logout) that the outbox
// (`resolveWorkerIdentity`) and the workspace-mirror identity read through the
// MsgDb — there is no "device secret" in this protocol.

import type { MsgDb } from './types'

/**
 * A tiny async KV for secrets. Values are opaque strings; `get` returns `null`
 * when absent (never `undefined` — the platform keychain APIs are null-shaped).
 */
export interface SecretStore {
  get(key: string): Promise<string | null>
  set(key: string, value: string): Promise<void>
  delete(key: string): Promise<void>
}

/**
 * The WEB adapter: secrets live in the MsgDb `meta` table — exactly where the
 * token was stored before M6-4, so the web target is byte-for-byte unchanged
 * (same Dexie rows, same clear-on-logout `metaPut(key, undefined)` shape).
 *
 * NEVER pair this with `SqliteDb` on the desktop: its `metaPut` refuses the
 * token key (fail-closed), which is the guard this seam exists to satisfy.
 */
export class MetaSecretStore implements SecretStore {
  constructor(private readonly db: MsgDb) {}

  async get(key: string): Promise<string | null> {
    const value = await this.db.metaGet<string>(key)
    return typeof value === 'string' ? value : null
  }

  set(key: string, value: string): Promise<void> {
    return this.db.metaPut(key, value)
  }

  delete(key: string): Promise<void> {
    // `metaPut(key, undefined)` mirrors the pre-M6-4 clearSession write exactly
    // (an undefined-valued row), keeping web storage byte-identical.
    return this.db.metaPut(key, undefined)
  }
}

/**
 * The in-memory adapter (tests + headless desktop CI): a plain Map, no
 * persistence. Stands in for the M6-5 OS-keychain implementation — the token
 * lives OUTSIDE the workspace folder by construction (it lives nowhere on disk).
 */
export class MemorySecretStore implements SecretStore {
  private readonly values = new Map<string, string>()

  get(key: string): Promise<string | null> {
    return Promise.resolve(this.values.get(key) ?? null)
  }

  set(key: string, value: string): Promise<void> {
    this.values.set(key, value)
    return Promise.resolve()
  }

  delete(key: string): Promise<void> {
    this.values.delete(key)
    return Promise.resolve()
  }
}
