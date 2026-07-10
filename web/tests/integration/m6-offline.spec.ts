// @vitest-environment node
//
// M6-4 (ENG-168) — THE OFFLINE GATE: drive the REAL WorkerCore in desktop
// trim (SqliteDb + full mirror + Node-fs seams + an injected SecretStore)
// through the offline-hardening contract:
//
//   1. ONLINE SEED — a live full-mirror session lands the workspace on disk
//      (M6-3 machinery, unchanged), with one attachment's bytes in the
//      content-addressed blob store (the "viewed while online" state).
//   2. COLD OFFLINE BOOT — a fresh core with `isOnline:false` and an
//      unreachable HTTP transport: `init()` restores the session from the
//      SecretStore (zero network), EVERY `query` verb answers from SQLite,
//      `search` answers from local FTS5, a send QUEUES in the outbox,
//      `readState.mark` advances locally, and each server-required verb
//      (admin/me/invites, uncached `file.fetch`) returns the CODED `offline`
//      error — asserted as a framed RPC error, never an unhandled throw.
//   3. RECONNECT — the rising edge into `live` drains the outbox, the sent
//      event appends to the NDJSON log via the mirror (WS echo path), the
//      offline read marker RE-PUSHES to the server, and the REAL
//      `msgctl verify --json <dir>` still exits 0.
//   4. TOKEN ABSENCE — the whole workspace folder (including the raw bytes of
//      projections.sqlite3 + its WAL) is scanned: the session token appears
//      NOWHERE (the folder is safe to hand to msgctl / zip / sync), proven
//      against a positive control that the scan really reads content.
//
// TOOLCHAIN GATING: same as the M6-3 gate — needs `uv` to spawn msgctl;
// self-skips where absent, hard-fails under CI.

import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import {
  buildChannelCreatedBody,
  buildFileUploadedBody,
  buildMessageCreatedBody,
  hashEvent,
  newDeviceId,
  newFileId,
  newStreamId,
  newUserId,
  newWorkspaceId,
  sha256Hex,
  type Body,
} from '../../src/core'
import { WorkerCore } from '../../src/worker/core'
import type { ApiResult, HttpClient } from '../../src/worker/http'
import { NodeBlobCache, NodeEventLog, NodeManifestStore } from '../../src/worker/mirror/node-fs'
import { WorkspaceMirror } from '../../src/worker/mirror/workspace-mirror'
import { MemorySecretStore } from '../../src/worker/secret-store'
import { openSqliteDb, type SqliteDb } from '../../src/worker/sqlite/sqlite-db'
import {
  META_DEVICE_ID,
  META_MY_USER_ID,
  META_PROJECTION_VERSION,
  META_ROLE,
  META_SESSION_EXPIRES_AT,
  META_SESSION_TOKEN,
  META_WORKSPACE_ID,
  PROJECTION_VERSION,
  type EventBody,
  type FromWorker,
  type MessagesListResult,
  type QueryParams,
  type RpcError,
  type RpcRequest,
  type SearchResult,
  type SendResult,
  type StreamsListResult,
  type SyncStatus,
  type WireEvent,
} from '../../src/worker/types'
import {
  collectingSink,
  FakeHttpClient,
  FakeSyncServer,
  makeFakeWsFactory,
  untilAsync,
} from '../unit/worker/helpers'

// ---------------------------------------------------------------------------
// Toolchain gating (uv → msgctl) — mirrors the M6-3 gate exactly.
// ---------------------------------------------------------------------------

const repoRoot = fileURLToPath(new URL('../../..', import.meta.url))
const uvAvailable = spawnSync('uv', ['--version'], { encoding: 'utf8' }).status === 0

if (!uvAvailable && process.env.CI) {
  throw new Error(
    'm6-offline gate: `uv` is not on PATH under CI. The web CI job must install uv ' +
      '(astral-sh/setup-uv) so `msgctl verify` can be spawned.',
  )
}

interface VerifyJson {
  ok: boolean
  workspace_id: string | null
  summary: { streams: number; events: number; failures: number; warnings: number }
  findings: { severity: string; class: string; detail: string }[]
}

function msgctlVerify(dir: string): { status: number; report: VerifyJson } {
  const res = spawnSync('uv', ['run', '--project', repoRoot, 'msgctl', 'verify', '--json', dir], {
    encoding: 'utf8',
    cwd: repoRoot,
    timeout: 180_000,
  })
  if (res.error) throw res.error
  let report: VerifyJson
  try {
    report = JSON.parse(res.stdout) as VerifyJson
  } catch {
    throw new Error(
      `msgctl verify emitted no JSON (exit ${res.status})\nstdout: ${res.stdout}\nstderr: ${res.stderr}`,
    )
  }
  return { status: res.status ?? -1, report }
}

// ---------------------------------------------------------------------------
// Offline-switchable HTTP: online → the FakeSyncServer; offline → every call
// is the transport-level fetch reject (status 0 / `network`), exactly what a
// dead link produces through the real createHttpClient.
// ---------------------------------------------------------------------------

class OfflineSwitchHttp implements HttpClient {
  online = true

  constructor(readonly inner: FakeHttpClient) {}

  private down<T>(): Promise<ApiResult<T>> {
    return Promise.resolve({
      ok: false,
      error: { status: 0, code: 'network', title: 'Network error' },
    })
  }

  get<T>(path: string): Promise<ApiResult<T>> {
    return this.online ? this.inner.get<T>(path) : this.down<T>()
  }
  post<T>(path: string, body: unknown): Promise<ApiResult<T>> {
    return this.online ? this.inner.post<T>(path, body) : this.down<T>()
  }
  put<T>(path: string, body: unknown): Promise<ApiResult<T>> {
    return this.online ? this.inner.put<T>(path, body) : this.down<T>()
  }
  patch<T>(path: string, body: unknown): Promise<ApiResult<T>> {
    return this.online ? this.inner.patch<T>(path, body) : this.down<T>()
  }
  del<T = void>(path: string): Promise<ApiResult<T>> {
    return this.online ? this.inner.del<T>(path) : this.down<T>()
  }
  putBlob(...args: Parameters<HttpClient['putBlob']>): ReturnType<HttpClient['putBlob']> {
    return this.online ? this.inner.putBlob(...args) : this.down<void>()
  }
  postBlob<T>(...args: Parameters<HttpClient['postBlob']>): Promise<ApiResult<T>> {
    return this.online ? this.inner.postBlob<T>(...args) : this.down<T>()
  }
  getBlob(path: string): ReturnType<HttpClient['getBlob']> {
    return this.online ? this.inner.getBlob(path) : this.down<{ blob: Blob; mimeType: string }>()
  }
}

// ---------------------------------------------------------------------------
// Workspace seed — real typed ULIDs, months strictly BEFORE the fake server's
// batch-accept month (2023-11) so live-drained events extend the log in order.
// ---------------------------------------------------------------------------

const wsId = newWorkspaceId()
const meUserId = newUserId()
const deviceId = newDeviceId()
const sMeta = newStreamId()
const sGeneral = newStreamId()
const fileIdCached = newFileId()
const fileIdUncached = newFileId()
const UNCACHED_SHA = 'b'.repeat(64)
const CACHED_BYTES = new Uint8Array([10, 20, 30, 40, 50])

/** The secret under test: must NEVER appear anywhere in the workspace folder. */
const TOKEN = 'tok_SECRET_M6_4_do_not_leak_1f2e3d'
/** Positive control: a string that MUST appear (proves the scan reads content). */
const CONTROL_TEXT = 'offline search target'

async function serve(body: Body, seq: number, receivedAt: string): Promise<WireEvent> {
  return {
    body: body as unknown as EventBody,
    event_hash: await hashEvent(body),
    signature: null,
    server: {
      server_sequence: seq,
      server_received_at: receivedAt,
      payload_redacted: false,
    },
  }
}

function at(month: string, second: number): string {
  return `${month}-05T10:00:${String(second).padStart(2, '0')}.000Z`
}

const common = {
  workspace_id: wsId,
  author_user_id: meUserId,
  author_device_id: deviceId,
}

async function seedServer(server: FakeSyncServer, cachedSha: string): Promise<void> {
  server.addStream({ stream_id: sMeta, kind: 'workspace-meta', name: null })
  server.addStream({ stream_id: sGeneral, name: 'general' })
  server.append(sMeta, [
    await serve(
      buildChannelCreatedBody({
        ...common,
        stream_id: sMeta,
        client_created_at: at('2023-09', 0),
        channel_stream_id: sGeneral,
        name: 'general',
        visibility: 'public',
      }),
      1,
      at('2023-09', 0),
    ),
  ])
  server.append(sGeneral, [
    await serve(
      buildMessageCreatedBody({
        ...common,
        stream_id: sGeneral,
        client_created_at: at('2023-09', 1),
        text: CONTROL_TEXT,
      }),
      1,
      at('2023-09', 1),
    ),
    await serve(
      buildMessageCreatedBody({
        ...common,
        stream_id: sGeneral,
        client_created_at: at('2023-09', 2),
        text: 'second message before the outage',
      }),
      2,
      at('2023-09', 2),
    ),
    await serve(
      buildFileUploadedBody({
        ...common,
        stream_id: sGeneral,
        client_created_at: at('2023-10', 0),
        file_id: fileIdCached,
        sha256: cachedSha,
        name: 'photo.bin',
        mime_type: 'application/octet-stream',
        size_bytes: CACHED_BYTES.length,
      }),
      3,
      at('2023-10', 0),
    ),
    await serve(
      buildFileUploadedBody({
        ...common,
        stream_id: sGeneral,
        client_created_at: at('2023-10', 1),
        file_id: fileIdUncached,
        sha256: UNCACHED_SHA,
        name: 'never-viewed.bin',
        mime_type: 'application/octet-stream',
        size_bytes: 9,
      }),
      4,
      at('2023-10', 1),
    ),
  ])
}

// ---------------------------------------------------------------------------
// Harness — REAL WorkerCore over SqliteDb + Node-fs seams + SecretStore
// ---------------------------------------------------------------------------

interface Harness {
  core: WorkerCore
  http: OfflineSwitchHttp
  ws: ReturnType<typeof makeFakeWsFactory>
  frames: Array<{ clientId: string; msg: FromWorker }>
}

let root: string
let db: SqliteDb
let server: FakeSyncServer
let blobCache: NodeBlobCache
const secrets = new MemorySecretStore()
let warnSpy: ReturnType<typeof vi.spyOn>

function boot(online: boolean): Harness {
  const mirror = new WorkspaceMirror(new NodeEventLog(root), new NodeManifestStore(root), {
    workspaceId: wsId,
    workspaceName: 'acme',
    myUserId: meUserId,
    deviceId,
  })
  const http = new OfflineSwitchHttp(new FakeHttpClient(server))
  http.online = online
  const ws = makeFakeWsFactory()
  const { sink, frames } = collectingSink()
  const core = new WorkerCore(db, sink, {
    http,
    wsFactory: ws.wsFactory,
    fullMirror: true,
    mirror,
    blobStore: blobCache,
    secretStore: secrets, // M6-4: the token rests OUTSIDE the folder
    isOnline: () => http.online,
  })
  return { core, http, ws, frames }
}

let rpcN = 0
async function rpc(
  h: Harness,
  req: RpcRequest,
): Promise<{ ok: true; result: unknown } | { ok: false; error: RpcError }> {
  const id = `r${++rpcN}`
  await h.core.handle('gate', { t: 'req', id, clientId: 'gate', req })
  const frame = h.frames.find((f) => f.msg.t === 'res' && f.msg.id === id)?.msg
  if (!frame || frame.t !== 'res') throw new Error(`no rpc response for ${req.method}`)
  return frame
}

async function rpcOk(h: Harness, req: RpcRequest): Promise<unknown> {
  const res = await rpc(h, req)
  if (!res.ok) throw new Error(`${req.method} failed: ${JSON.stringify(res.error)}`)
  return res.result
}

async function rpcErrCode(h: Harness, req: RpcRequest): Promise<string> {
  const res = await rpc(h, req)
  if (res.ok) throw new Error(`${req.method} unexpectedly succeeded`)
  return res.error.code
}

async function stopSync(h: Harness): Promise<void> {
  await rpcOk(h, { method: 'sync.stop', params: {} })
}

async function cursorAt(streamId: string, seq: number): Promise<boolean> {
  return (await db.getCursor(streamId))?.last_contiguous_seq === seq
}

/** Every file under `dir` (recursive) whose RAW BYTES contain `needle`. */
function filesContaining(dir: string, needle: string): string[] {
  const hits: string[] = []
  const walk = (p: string): void => {
    for (const entry of readdirSync(p, { withFileTypes: true })) {
      const full = join(p, entry.name)
      if (entry.isDirectory()) walk(full)
      else if (readFileSync(full).includes(needle)) hits.push(full)
    }
  }
  walk(dir)
  return hits
}

// ---------------------------------------------------------------------------
// The gate
// ---------------------------------------------------------------------------

describe.skipIf(!uvAvailable)(
  'M6-4 offline gate — SecretStore + cold-boot offline + degradation + reconnect',
  () => {
    let hOffline: Harness

    beforeAll(async () => {
      warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
      vi.stubGlobal('location', { protocol: 'http:', host: 'gate.test' })
      root = mkdtempSync(join(tmpdir(), 'msg-m6-offline-'))
      db = await openSqliteDb(join(root, 'projections.sqlite3'))
      blobCache = new NodeBlobCache(root)

      // Session seed: identity in meta (non-secret), the TOKEN in the SecretStore.
      await db.metaPut(META_PROJECTION_VERSION, PROJECTION_VERSION)
      await secrets.set(META_SESSION_TOKEN, TOKEN)
      await db.metaPut(META_MY_USER_ID, meUserId)
      await db.metaPut(META_WORKSPACE_ID, wsId)
      await db.metaPut(META_ROLE, 'member')
      await db.metaPut(META_SESSION_EXPIRES_AT, '2099-01-01T00:00:00Z')
      await db.metaPut(META_DEVICE_ID, deviceId)

      server = new FakeSyncServer()
      await seedServer(server, await sha256Hex(CACHED_BYTES))
    }, 60_000)

    afterAll(async () => {
      warnSpy.mockRestore()
      vi.unstubAllGlobals()
      await db.close()
      rmSync(root, { recursive: true, force: true })
    })

    it('ONLINE SEED: a live session mirrors the workspace; one blob is cached locally', async () => {
      const h = boot(true)
      await h.core.init()
      h.ws.last().open()
      await untilAsync(async () => (await cursorAt(sMeta, 1)) && (await cursorAt(sGeneral, 4)))
      await stopSync(h)

      // The "user viewed this attachment while online" state — the M6-3 tee's
      // outcome, written through the same content-addressed store it fills.
      await blobCache.put(await sha256Hex(CACHED_BYTES), CACHED_BYTES)
      expect(await blobCache.has(await sha256Hex(CACHED_BYTES))).toBe(true)
    }, 60_000)

    it('COLD OFFLINE BOOT: init restores the session from the SecretStore, no dial', async () => {
      hOffline = boot(false)
      await hOffline.core.init()

      expect(hOffline.ws.sockets).toHaveLength(0) // never dialed
      const status = (await rpcOk(hOffline, { method: 'auth.status', params: {} })) as {
        ok: boolean
        status: { authenticated: boolean; my_user_id?: string }
      }
      expect(status.status.authenticated).toBe(true)
      expect(status.status.my_user_id).toBe(meUserId)

      const sync = (await rpcOk(hOffline, { method: 'sync.status', params: {} })) as SyncStatus
      expect(sync.state).toBe('degraded')
      expect(sync.online).toBe(false)
    })

    it('OFFLINE: every query verb answers from SQLite (full-mirror history)', async () => {
      const streams = (await rpcOk(hOffline, {
        method: 'query',
        params: { q: 'streams.list' },
      })) as StreamsListResult
      expect(streams.streams.map((s) => s.stream_id).sort()).toEqual([sGeneral, sMeta].sort())

      const messages = (await rpcOk(hOffline, {
        method: 'query',
        params: { q: 'messages.list', stream_id: sGeneral },
      })) as MessagesListResult
      expect(messages.messages).toHaveLength(2)
      expect(messages.messages.map((m) => m.text)).toContain(CONTROL_TEXT)
      const anchor = messages.messages.find((m) => m.text === CONTROL_TEXT)
      if (!anchor) throw new Error('anchor message missing')

      // The full local read surface — each verb must ANSWER offline (ok frame).
      const queries: QueryParams[] = [
        { q: 'message.get', message_id: anchor.message_id },
        { q: 'directory.list' },
        { q: 'workspace.info' },
        { q: 'files.list' },
        { q: 'attachments.forMessage', message_id: anchor.message_id },
        { q: 'messages.reactions', message_ids: [anchor.message_id] },
        { q: 'messages.threads', root_message_ids: [anchor.message_id] },
        { q: 'messages.thread', root_message_id: anchor.message_id },
      ]
      for (const params of queries) {
        await rpcOk(hOffline, { method: 'query', params })
      }

      const files = (await rpcOk(hOffline, {
        method: 'query',
        params: { q: 'files.list' },
      })) as { files: { file_id: string }[] }
      expect(files.files.map((f) => f.file_id).sort()).toEqual(
        [fileIdCached, fileIdUncached].sort(),
      )
    })

    it('OFFLINE: `search` answers from local FTS5 with zero network', async () => {
      const result = (await rpcOk(hOffline, {
        method: 'search',
        params: { q: 'target' },
      })) as SearchResult
      expect(result.hits).toHaveLength(1)
      expect(result.hits[0]?.text).toBe(CONTROL_TEXT)
      expect(hOffline.http.inner.countGets('/v1/search')).toBe(0)
    })

    it('OFFLINE: a send QUEUES; the pending row renders; readState.mark advances locally', async () => {
      const send = (await rpcOk(hOffline, {
        method: 'mutate',
        params: { m: 'outbox.send', stream_id: sGeneral, text: 'sent while offline' },
      })) as SendResult
      expect(send.message_id).toBeTruthy()
      expect(await db.count('outbox')).toBe(1)

      const messages = (await rpcOk(hOffline, {
        method: 'query',
        params: { q: 'messages.list', stream_id: sGeneral },
      })) as MessagesListResult
      const pending = messages.messages.find((m) => m.message_id === send.message_id)
      expect(pending?.state).toBe('pending')

      const marked = (await rpcOk(hOffline, {
        method: 'readState.mark',
        params: { stream_id: sGeneral, last_read_seq: 4 },
      })) as { stream_id: string; last_read_seq: number }
      expect(marked).toEqual({ stream_id: sGeneral, last_read_seq: 4 })
      expect((await db.getReadState(sGeneral))?.last_read_seq).toBe(4)

      // Ephemeral signals stay structural no-ops offline — never an error.
      expect(
        await rpcOk(hOffline, { method: 'typing.send', params: { stream_id: sGeneral } }),
      ).toEqual({ ok: true })
    })

    it('OFFLINE degradation matrix: server-required verbs return the CODED `offline` error', async () => {
      expect(await rpcErrCode(hOffline, { method: 'admin.members.list', params: {} })).toBe(
        'offline',
      )
      expect(await rpcErrCode(hOffline, { method: 'admin.invites.list', params: {} })).toBe(
        'offline',
      )
      expect(
        await rpcErrCode(hOffline, {
          method: 'admin.invites.create',
          params: { role: 'member' },
        }),
      ).toBe('offline')
      expect(await rpcErrCode(hOffline, { method: 'me.get', params: {} })).toBe('offline')
      expect(
        await rpcErrCode(hOffline, {
          method: 'me.update',
          params: { display_name: 'New Name' },
        }),
      ).toBe('offline')
      // An attachment never viewed online → its bytes are simply not here.
      expect(
        await rpcErrCode(hOffline, {
          method: 'file.fetch',
          params: { file_id: fileIdUncached, variant: 'blob' },
        }),
      ).toBe('offline')
    })

    it('OFFLINE: a previously-viewed attachment serves from the content-addressed store', async () => {
      const result = (await rpcOk(hOffline, {
        method: 'file.fetch',
        params: { file_id: fileIdCached, variant: 'blob' },
      })) as { blob: Blob | null; mime_type?: string }
      expect(result.blob).not.toBeNull()
      expect(result.blob?.size).toBe(CACHED_BYTES.length)
      expect(result.mime_type).toBe('application/octet-stream')
      expect(hOffline.http.inner.getBlobCalls).toHaveLength(0)
    })

    it('RECONNECT: outbox drains, the event appends to NDJSON, the read marker re-pushes', async () => {
      hOffline.http.online = true
      hOffline.core.notifyOnline() // degraded → connecting (dials the fake ws)
      hOffline.ws.last().open() // → syncing → live → rising edge

      // Rising edge into live: the queued send drains…
      await untilAsync(async () => (await db.count('outbox')) === 0)
      // …and the offline read marker re-pushes (server had never seen it).
      await untilAsync(() => Promise.resolve(server.readState.get(sGeneral) === 4))

      // The server echoes the accepted event over WS (as it does to every
      // connection of the workspace) — the mirror appends it durably.
      const wire = server.events(sGeneral)[4]
      if (!wire) throw new Error('drained event not on the server')
      hOffline.ws.last().emitEvent(wire)
      await untilAsync(async () => cursorAt(sGeneral, 5))

      const messages = (await rpcOk(hOffline, {
        method: 'query',
        params: { q: 'messages.list', stream_id: sGeneral },
      })) as MessagesListResult
      const sent = messages.messages.find((m) => m.text === 'sent while offline')
      expect(sent?.state).toBeUndefined() // settled, no longer pending
      expect(sent?.created_seq).toBe(5)

      await stopSync(hOffline)

      // The on-disk log now carries the offline-composed event, gapless.
      const log = new NodeEventLog(root)
      const seqs = (await log.readAll(sGeneral)).map(
        (l) => (JSON.parse(l) as { server: { server_sequence: number } }).server.server_sequence,
      )
      expect(seqs).toEqual([1, 2, 3, 4, 5])
    }, 60_000)

    it('the folder still verifies: msgctl verify exits 0 with zero failures', () => {
      const { status, report } = msgctlVerify(root)
      expect(report.findings.filter((f) => f.severity === 'failure')).toEqual([])
      expect(report.ok).toBe(true)
      expect(report.summary.failures).toBe(0)
      expect(report.summary.events).toBe(6) // 5 seeded + the offline-composed send
      expect(report.summary.streams).toBe(2)
      expect(report.workspace_id).toBe(wsId)
      expect(status).toBe(0)
    }, 120_000)

    it('TOKEN ABSENCE: the session token appears NOWHERE in the workspace folder', () => {
      // Positive control first — the scan demonstrably reads real content
      // (the message text lives in the NDJSON log and the projections DB).
      expect(filesContaining(root, CONTROL_TEXT).length).toBeGreaterThan(0)
      // The token is in the SecretStore only — zero hits across every byte of
      // every file, including projections.sqlite3 and its WAL sidecars.
      expect(filesContaining(root, TOKEN)).toEqual([])
    })
  },
)
