// tests/unit/worker/me.spec.ts — the self-profile RPC arms: HTTP pass-through
// over the worker's authed client (`/v1/me`), nothing persisted locally.
// Asserts each handler issues the right method + URL + body (structurally
// self-only — no user_id anywhere), success returns the server shape verbatim,
// 401/422 fold into the standard coded RPC errors, plus a WorkerCore round
// trip and the token boundary.

import { describe, expect, it } from 'vitest'

import { getMe, updateMe } from '../../../src/worker/me'
import { WorkerCore } from '../../../src/worker/core'
import { MemoryDb } from '../../../src/worker/db'
import { RpcCodedError } from '../../../src/worker/types'
import type { FromWorker, MeProfile } from '../../../src/worker/types'
import type { ApiError } from '../../../src/worker/http'

import { collectingSink, FakeHttpClient, FakeSyncServer, inertWsFactory } from './helpers'

function profile(overrides: Partial<MeProfile> = {}): MeProfile {
  return {
    user_id: 'u_me',
    display_name: 'Alice',
    email: 'alice@example.com',
    role: 'member',
    is_bot: false,
    ...overrides,
  }
}

const unauthenticated: ApiError = { status: 401, code: 'unauthenticated', title: 'Unauthenticated' }
const invalid: ApiError = { status: 422, code: 'validation-error', title: 'Validation error' }

async function codeOf(p: Promise<unknown>): Promise<string> {
  try {
    await p
  } catch (err) {
    if (err instanceof RpcCodedError) return err.code
    throw err
  }
  throw new Error('expected a rejection')
}

describe('me.get (GET /v1/me)', () => {
  it('GETs the profile and returns the server fields verbatim', async () => {
    const server = new FakeSyncServer()
    server.meProfile = profile()
    const http = new FakeHttpClient(server)

    const res = await getMe(http)

    expect(http.getCalls).toEqual(['/v1/me'])
    expect(res).toEqual({
      user_id: 'u_me',
      display_name: 'Alice',
      email: 'alice@example.com',
      role: 'member',
      is_bot: false,
    })
  })

  it('maps a 401 (dead session) to the coded `unauthenticated` error', async () => {
    const server = new FakeSyncServer()
    server.meError = unauthenticated
    const http = new FakeHttpClient(server)

    expect(await codeOf(getMe(http))).toBe('unauthenticated')
  })
})

describe('me.update (PATCH /v1/me)', () => {
  it('PATCHes /v1/me with ONLY display_name and returns the updated profile', async () => {
    const server = new FakeSyncServer()
    server.meProfile = profile()
    const http = new FakeHttpClient(server)

    const res = await updateMe(http, { display_name: 'Alice Renamed' })

    // Structurally self-only: no user_id in the path or the body.
    expect(http.patchCalls).toEqual([{ path: '/v1/me', body: { display_name: 'Alice Renamed' } }])
    expect(res.display_name).toBe('Alice Renamed')
    expect(res.user_id).toBe('u_me')
    // The fake server's profile advanced (a subsequent GET sees the rename).
    expect((await getMe(http)).display_name).toBe('Alice Renamed')
  })

  it('maps a 422 (empty/oversized name) to the coded `validation-error`', async () => {
    const server = new FakeSyncServer()
    server.meProfile = profile()
    server.meError = invalid
    const http = new FakeHttpClient(server)

    expect(await codeOf(updateMe(http, { display_name: '' }))).toBe('validation-error')
  })

  it('maps a 401 to the coded `unauthenticated` error', async () => {
    const server = new FakeSyncServer()
    server.meError = unauthenticated
    const http = new FakeHttpClient(server)

    expect(await codeOf(updateMe(http, { display_name: 'X' }))).toBe('unauthenticated')
  })
})

describe('token boundary (R1)', () => {
  it('me params/paths never carry a token/bearer/authorization field', async () => {
    const server = new FakeSyncServer()
    server.meProfile = profile()
    const http = new FakeHttpClient(server)

    await getMe(http)
    await updateMe(http, { display_name: 'Renamed' })

    const surfaces = [
      ...http.getCalls,
      ...http.patchCalls.map((c) => c.path + JSON.stringify(c.body)),
    ]
    for (const s of surfaces) {
      expect(s).not.toMatch(/token|bearer|authorization/i)
    }
  })
})

describe('me RPC (WorkerCore round trip)', () => {
  it('answers `me.get` with the profile (plain data only)', async () => {
    const server = new FakeSyncServer()
    server.meProfile = profile()
    const http = new FakeHttpClient(server)
    const { sink, frames } = collectingSink()
    const core = new WorkerCore(new MemoryDb(), sink, { http, wsFactory: inertWsFactory })
    await core.init()

    await core.handle('c1', {
      t: 'req',
      id: 'a1',
      clientId: 'c1',
      req: { method: 'me.get', params: {} },
    })

    const res = lastRes(frames, 'a1')
    expect(res.t === 'res' && res.ok).toBe(true)
    if (res.t === 'res' && res.ok) {
      const result = res.result as MeProfile
      expect(result.user_id).toBe('u_me')
      // The frame carries plain profile data — nothing token-ish.
      expect(JSON.stringify(result)).not.toMatch(/token|bearer|authorization/i)
    }
    expect(http.getCalls).toContain('/v1/me')
  })

  it('answers `me.update` and frames a 422 as a structured coded error', async () => {
    const server = new FakeSyncServer()
    server.meProfile = profile()
    const http = new FakeHttpClient(server)
    const { sink, frames } = collectingSink()
    const core = new WorkerCore(new MemoryDb(), sink, { http, wsFactory: inertWsFactory })
    await core.init()

    await core.handle('c1', {
      t: 'req',
      id: 'a2',
      clientId: 'c1',
      req: { method: 'me.update', params: { display_name: 'New Name' } },
    })
    const ok = lastRes(frames, 'a2')
    expect(ok.t === 'res' && ok.ok).toBe(true)
    if (ok.t === 'res' && ok.ok) {
      expect((ok.result as MeProfile).display_name).toBe('New Name')
    }

    server.meError = invalid
    await core.handle('c1', {
      t: 'req',
      id: 'a3',
      clientId: 'c1',
      req: { method: 'me.update', params: { display_name: '' } },
    })
    const bad = lastRes(frames, 'a3')
    expect(bad.t === 'res' && !bad.ok).toBe(true)
    if (bad.t === 'res' && !bad.ok) {
      expect(bad.error.code).toBe('validation-error')
    }
  })
})

function lastRes(frames: Array<{ clientId: string; msg: FromWorker }>, id: string): FromWorker {
  const found = frames.find((f) => f.msg.t === 'res' && f.msg.id === id)?.msg
  if (!found) throw new Error(`no res frame for id ${id}`)
  return found
}
