// PERMANENT GATE — §12 invariant 6, CLIENT (Dexie) side: rebuild ≡ incremental,
// PROPERTY-BASED. The Dexie analogue of the permanent server equivalence gate.
//
// fast-check generates randomized event histories across streams — message.created
// v1 (unicode / optional thread_root_id / mentions) AND (ENG-100, M3) the reaction
// add/remove set, message.edited (LWW), message.deleted (tombstone + redact), and
// threaded replies (some later deleted) — interleaved with D9-skip events (unknown
// types, v>=2, meta, malformed-known). It ALSO generates randomized outbox rows:
// pending / failed message.created AND the M3 optimistic OVERLAY ops
// (react/edit/delete of a settled message), plus the crash-orphaned "settled but
// still in outbox" case. It applies the history incrementally through the REAL
// `applyEventsToProjection` + the REAL pending overlay, snapshots the REAL
// projection dump (messages incl. edited_seq/deleted/reply_count/format + the
// reactions set + thread participants + the pending lifecycle), drops the derived
// tables, rebuilds through the REAL `rebuildProjections` (events replay + outbox
// re-derive) and asserts the rebuilt dump is BYTE-EQUAL to the incremental one.
//
// The property loop runs against MemoryDb for breadth; a dedicated gating
// assertion ALSO replays a drawn history through the real DexieDb (fake-indexeddb)
// so the SHIPPING IndexedDB rebuild path is what is gated.
//
// ENG-120 EXTENSION: the generated histories ALSO emit `file.uploaded` events
// (interleaved with message.created that reference them via `file_ids`), and the
// gate dump appends the `files` set (via `dumpFiles`) + the message rows'
// `file_ids`. Because `file.uploaded` is an IMMUTABLE keyed upsert, it converges
// under the windowed (newest-first + backfill) delivery for free — a duplicate is
// byte-identical and arrival before/after its message.created does not matter.
//
// TEETH (all rebuild-pass-only, ENG-61 pattern):
//   MSG_MUTATE=inv6-rebuild-skew    — corrupt one message.created row's text.
//   MSG_MUTATE=inv6-delete-skew     — rebuild "forgets" the delete tombstone.
//   MSG_MUTATE=inv6-reaction-skew   — rebuild ignores reaction removes (blind add).
//   MSG_MUTATE=inv6-file-skew       — corrupt one file.uploaded row's name.
// Each makes rebuild != incremental → RED. Unset (CI default) the suite is green.
// Plus deterministic file teeth (no env var): a NON-idempotent/order-dependent
// file handler makes windowed != in-order rebuild → RED (Property 2f).

import fc from 'fast-check'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { openDb, MemoryDb, rebuildProjections } from '../../../src/worker/db'
import { applyPendingOutboxRow } from '../../../src/worker/outbox'
import {
  applyEventsToProjection,
  applyFileUploadedV1,
  applyMessageCreatedV1,
  dumpFiles,
  dumpMessages,
  FILE_HANDLERS,
  HANDLERS,
} from '../../../src/worker/projection'
import type {
  EventRow,
  FileRow,
  MessageRow,
  MsgDb,
  OutboxRow,
  ReactionRow,
  ThreadParticipantRow,
} from '../../../src/worker/types'

import { fakeIdbOptions } from './helpers'
import {
  fileId,
  fileUploadedEvent,
  malformedMessageEvent,
  messageCreatedEvent,
  messageDeletedEvent,
  messageEditedEvent,
  metaEvent,
  reactionAddedEvent,
  reactionRemovedEvent,
  unknownTypeEvent,
} from './projfixtures'

const MUTATION = process.env.MSG_MUTATE

// ---------------------------------------------------------------------------
// GATE-ONLY comprehensive dump. The shipping `dumpMessages` deliberately omits
// the lifecycle + M3 fields (they are re-derivable, so rebuild ≡ incremental
// holds without them) and reads only `messages`. We must NOT change it. This
// test-only dump reuses the shipping ordering + JSON discipline and appends the
// M3 message columns (edited_seq/deleted/reply_count/last_reply_seq) + the
// lifecycle (state/error_code), THEN the reactions set + thread participants, so
// an incremental-vs-rebuild divergence in ANY of them turns the gate RED.
// ---------------------------------------------------------------------------

function compareForDump(a: MessageRow, b: MessageRow): number {
  if (a.stream_id !== b.stream_id) return a.stream_id < b.stream_id ? -1 : 1
  if (a.created_seq !== b.created_seq) return a.created_seq - b.created_seq
  if (a.message_id !== b.message_id) return a.message_id < b.message_id ? -1 : 1
  return 0
}

function cmpStr(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

function compareReaction(a: ReactionRow, b: ReactionRow): number {
  return (
    cmpStr(a.message_id, b.message_id) ||
    cmpStr(a.emoji, b.emoji) ||
    cmpStr(a.author_user_id, b.author_user_id)
  )
}

function compareParticipant(a: ThreadParticipantRow, b: ThreadParticipantRow): number {
  return cmpStr(a.root_message_id, b.root_message_id) || cmpStr(a.user_id, b.user_id)
}

async function dumpProjection(db: MsgDb): Promise<string> {
  const rows = await db.getAllMessages()
  rows.sort(compareForDump)
  const messageLines = rows.map((row) =>
    JSON.stringify({
      message_id: row.message_id,
      stream_id: row.stream_id,
      created_seq: row.created_seq,
      author_user_id: row.author_user_id,
      text: row.text,
      format: row.format,
      thread_root_id: row.thread_root_id ?? null,
      mention_user_ids: row.mention_user_ids,
      file_ids: row.file_ids, // ENG-120 attachment linkage (gate-only; not in dumpMessages)
      edited_seq: row.edited_seq ?? null, // M3
      deleted: row.deleted ?? false, // M3
      reply_count: row.reply_count ?? 0, // M3 threads
      last_reply_seq: row.last_reply_seq ?? null, // M3 threads
      state: row.state ?? null, // lifecycle (gate-only)
      error_code: row.error_code ?? null, // lifecycle (gate-only)
    }),
  )

  // OBSERVABLE reactions only: `present` keys (a tombstone `present:false` is a
  // removed reaction, not shown). The present set is order-independent (present iff
  // highest-seq event is an add), so this stays byte-stable across delivery orders.
  const reactions = (await db.getAllReactions()).filter((r) => r.present)
  reactions.sort(compareReaction)
  const reactionLines = reactions.map((r) =>
    JSON.stringify({ message_id: r.message_id, emoji: r.emoji, author_user_id: r.author_user_id }),
  )

  const participants = await db.getAllThreadParticipants()
  participants.sort(compareParticipant)
  const participantLines = participants.map((p) =>
    JSON.stringify({ root_message_id: p.root_message_id, user_id: p.user_id }),
  )

  // ENG-120: the `files` set — the NEW rebuild ≡ incremental surface. `dumpFiles`
  // sorts by file_id with a stable field order, so any incremental-vs-rebuild
  // divergence in the keyed-upsert files projection turns the gate RED.
  const filesDump = await dumpFiles(db)

  return [
    'MESSAGES',
    ...messageLines,
    'REACTIONS',
    ...reactionLines,
    'PARTICIPANTS',
    ...participantLines,
    'FILES',
    filesDump,
  ].join('\n')
}

// The D9 skip (malformed / unknown / v>=2 events) warns by design; randomized
// histories generate many, so silence the expected noise for readable CI logs.
beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
  HANDLERS['message.created@1'] = applyMessageCreatedV1 // restore after any teeth patch
  FILE_HANDLERS['file.uploaded@1'] = applyFileUploadedV1
})

// ---------------------------------------------------------------------------
// Randomized-history materialization. fast-check draws SHAPE choices; a mutable
// counter mints unique ids + per-stream ascending sequences, and target-needing
// ops (reply/react/edit/delete) reference a message ALREADY created in the SAME
// stream (valid references only — cross-stream/reply-of-reply are server-rejected),
// so every history is well-formed yet fully random.
// ---------------------------------------------------------------------------

const NUM_STREAMS = 3
const EMOJIS = ['👍', '🎉', '☕', 'x', 'x'] // two distinct 'x' bytes — opaque key
const USERS = ['u_a', 'u_b', 'u_c']

type EventKind =
  | 'create'
  | 'reply'
  | 'react-add'
  | 'react-remove'
  | 'edit'
  | 'delete'
  | 'file' // ENG-120: a file.uploaded event (a fresh file id in the stream)
  | 'unknown'
  | 'meta'
  | 'malformed'

interface EventChoice {
  stream: number
  kind: EventKind
  text: string
  format: 'markdown' | 'plain'
  mentions: string[]
  emoji: number
  reactor: number
  target: number
}

type OutboxKind = 'create' | 'react-add' | 'react-remove' | 'edit' | 'delete'
interface OutboxChoice {
  stream: number
  kind: OutboxKind
  state: OutboxRow['state']
  text: string
  emoji: number
  reactor: number
  target: number
  orphan: boolean
  errorCode: string | null
}

interface History {
  byStream: Map<string, EventRow[]>
  outbox: OutboxRow[]
}

const TEXTS = fc.oneof(
  fc.string(),
  fc.constantFrom('', 'unicode 日本語 🎉 ☕', 'multi\nline\ttext', '"quotes" & <html>'),
)

const eventChoiceArb: fc.Arbitrary<EventChoice> = fc.record({
  stream: fc.integer({ min: 0, max: NUM_STREAMS - 1 }),
  kind: fc.constantFrom<EventKind>(
    'create',
    'create',
    'reply',
    'react-add',
    'react-add',
    'react-remove',
    'edit',
    'delete',
    'file',
    'file',
    'unknown',
    'meta',
    'malformed',
  ),
  text: TEXTS,
  format: fc.constantFrom('markdown', 'plain'),
  mentions: fc.array(fc.constantFrom('u_x', 'u_y', 'u_z'), { maxLength: 3 }),
  emoji: fc.integer({ min: 0, max: EMOJIS.length - 1 }),
  reactor: fc.integer({ min: 0, max: USERS.length - 1 }),
  target: fc.integer({ min: 0, max: 999 }),
})

const outboxChoiceArb: fc.Arbitrary<OutboxChoice> = fc.record({
  stream: fc.integer({ min: 0, max: NUM_STREAMS - 1 }),
  kind: fc.constantFrom<OutboxKind>('create', 'react-add', 'react-remove', 'edit', 'delete'),
  state: fc.constantFrom<OutboxRow['state']>('queued', 'sending', 'rejected'),
  text: TEXTS,
  emoji: fc.integer({ min: 0, max: EMOJIS.length - 1 }),
  reactor: fc.integer({ min: 0, max: USERS.length - 1 }),
  target: fc.integer({ min: 0, max: 999 }),
  orphan: fc.boolean(),
  errorCode: fc.option(fc.constantFrom('permission_denied', 'payload_too_large'), { nil: null }),
})

const historyArb: fc.Arbitrary<{ events: EventChoice[]; outbox: OutboxChoice[] }> = fc.record({
  events: fc.array(eventChoiceArb, { maxLength: 20 }),
  outbox: fc.array(outboxChoiceArb, { maxLength: 8 }),
})

/** A body for an M3-reference outbox row (reaction/edit/delete of a target). */
function refBody(
  type: string,
  streamId: string,
  eventId: string,
  author: string,
  payload: Record<string, unknown>,
): Record<string, unknown> {
  return {
    event_id: eventId,
    workspace_id: 'w_test',
    stream_id: streamId,
    type,
    type_version: 1,
    author_user_id: author,
    author_device_id: 'd_test',
    client_created_at: '2026-01-01T00:00:00.000Z',
    payload,
  }
}

/** A message.created outbox body (the shape the send path mints). */
function createdBody(
  streamId: string,
  eventId: string,
  messageId: string,
  text: string,
): Record<string, unknown> {
  return refBody('message.created', streamId, eventId, 'u_author', {
    message_id: messageId,
    text,
    format: 'markdown',
    thread_root_id: null,
    file_ids: [],
    mentions: [],
  })
}

/** Materialize drawn shape choices into a well-formed, unique-id history. */
function materialize(draw: { events: EventChoice[]; outbox: OutboxChoice[] }): History {
  const streamIds = Array.from({ length: NUM_STREAMS }, (_, i) => `s_${i}`)
  const seqOf = new Array<number>(NUM_STREAMS).fill(0)
  const byStream = new Map<string, EventRow[]>(streamIds.map((s) => [s, []]))
  // Per-stream: message ids created so far (settled), + the subset that are roots.
  const createdByStream: string[][] = streamIds.map(() => [])
  const rootsByStream: string[][] = streamIds.map(() => [])
  // ENG-120: file ids uploaded so far per stream (a create may reference them).
  const filesByStream: string[][] = streamIds.map(() => [])
  const settledCreatedIds: { streamId: string; messageId: string; eventId: string }[] = []
  let id = 0
  let fileCounter = 0

  const push = (streamId: string, ev: EventRow): void => {
    byStream.get(streamId)!.push(ev)
  }

  for (const ec of draw.events) {
    const streamId = streamIds[ec.stream]!
    const created = createdByStream[ec.stream]!
    const roots = rootsByStream[ec.stream]!
    const files = filesByStream[ec.stream]!
    // Degrade a target-needing op to a plain create when the stream has no target.
    let kind = ec.kind
    if (
      (kind === 'react-add' || kind === 'react-remove' || kind === 'edit' || kind === 'delete') &&
      created.length === 0
    ) {
      kind = 'create'
    }
    if (kind === 'reply' && roots.length === 0) kind = 'create'
    const seq = ++seqOf[ec.stream]!

    if (kind === 'unknown') {
      push(streamId, unknownTypeEvent(streamId, seq))
      continue
    }
    if (kind === 'meta') {
      push(streamId, metaEvent(streamId, seq))
      continue
    }
    if (kind === 'malformed') {
      push(streamId, malformedMessageEvent(streamId, seq))
      continue
    }
    if (kind === 'file') {
      // A fresh, globally-unique file id (each file is uploaded exactly once).
      const fid = fileId(++fileCounter)
      push(
        streamId,
        fileUploadedEvent({
          streamId,
          seq,
          fileId: fid,
          name: `f${fileCounter}.png`,
          mimeType: ec.emoji % 2 === 0 ? 'image/png' : 'application/pdf',
          sizeBytes: ec.target,
        }),
      )
      files.push(fid)
      continue
    }
    // A create/reply may REFERENCE a subset of the stream's already-uploaded files
    // (0..N of them, picked deterministically) — the message→attachment linkage.
    const attach = (): string[] =>
      files.length === 0 ? [] : files.slice(0, ec.target % (files.length + 1))
    if (kind === 'create') {
      const messageId = `m_${++id}`
      push(
        streamId,
        messageCreatedEvent({
          streamId,
          seq,
          messageId,
          text: ec.text,
          format: ec.format,
          mentions: ec.mentions,
          fileIds: attach(),
        }),
      )
      created.push(messageId)
      roots.push(messageId)
      settledCreatedIds.push({ streamId, messageId, eventId: `e_${streamId}_${seq}` })
      continue
    }
    if (kind === 'reply') {
      const root = roots[ec.target % roots.length]!
      const messageId = `m_${++id}`
      push(
        streamId,
        messageCreatedEvent({
          streamId,
          seq,
          messageId,
          text: ec.text,
          threadRootId: root,
          authorUserId: USERS[ec.reactor]!,
          fileIds: attach(),
        }),
      )
      created.push(messageId)
      settledCreatedIds.push({ streamId, messageId, eventId: `e_${streamId}_${seq}` })
      continue
    }
    const target = created[ec.target % created.length]!
    if (kind === 'react-add') {
      push(
        streamId,
        reactionAddedEvent({
          streamId,
          seq,
          messageId: target,
          emoji: EMOJIS[ec.emoji]!,
          authorUserId: USERS[ec.reactor]!,
        }),
      )
    } else if (kind === 'react-remove') {
      push(
        streamId,
        reactionRemovedEvent({
          streamId,
          seq,
          messageId: target,
          emoji: EMOJIS[ec.emoji]!,
          authorUserId: USERS[ec.reactor]!,
        }),
      )
    } else if (kind === 'edit') {
      push(
        streamId,
        messageEditedEvent({ streamId, seq, messageId: target, text: ec.text, format: ec.format }),
      )
    } else if (kind === 'delete') {
      push(streamId, messageDeletedEvent({ streamId, seq, messageId: target }))
    }
  }

  // ---- Outbox: pending message.created + M3 optimistic overlay ops -----------
  const outbox: OutboxRow[] = []
  for (const oc of draw.outbox) {
    const isOrphan = oc.orphan && settledCreatedIds.length > 0
    if (isOrphan) {
      // Reuse a settled message.created event's ids: the incremental state has the
      // SETTLED row (from events) + a lingering outbox row; rebuild must skip it.
      const s = settledCreatedIds[id % settledCreatedIds.length]!
      outbox.push({
        event_id: s.eventId,
        created_at: 1_000 + id,
        body: createdBody(s.streamId, s.eventId, s.messageId, 'stale-orphan'),
        event_hash: `sha256:${s.eventId}`,
        message_id: s.messageId,
        stream_id: s.streamId,
        state: oc.state,
      })
      id++
      continue
    }
    // A pending overlay op targeting a SETTLED message in the chosen stream (the
    // realistic "latest local action" layered on top of the settled base).
    const streamId = streamIds[oc.stream]!
    const targets = settledCreatedIds.filter((s) => s.streamId === streamId)
    const eventId = `o_${++id}`
    const createdAt = 1_700_000_000_000 + id
    let kind: OutboxKind = oc.kind
    if (kind !== 'create' && targets.length === 0) kind = 'create'
    let body: Record<string, unknown>
    let messageId: string
    if (kind === 'create') {
      messageId = `m_o_${id}`
      body = createdBody(streamId, eventId, messageId, oc.text)
    } else {
      messageId = targets[oc.target % targets.length]!.messageId
      const author = USERS[oc.reactor]!
      if (kind === 'react-add') {
        body = refBody('reaction.added', streamId, eventId, author, {
          message_id: messageId,
          emoji: EMOJIS[oc.emoji]!,
        })
      } else if (kind === 'react-remove') {
        body = refBody('reaction.removed', streamId, eventId, author, {
          message_id: messageId,
          emoji: EMOJIS[oc.emoji]!,
        })
      } else if (kind === 'edit') {
        body = refBody('message.edited', streamId, eventId, 'u_author', {
          message_id: messageId,
          text: oc.text,
          format: oc.emoji % 2 === 0 ? 'markdown' : 'plain',
        })
      } else {
        body = refBody('message.deleted', streamId, eventId, 'u_author', { message_id: messageId })
      }
    }
    outbox.push({
      event_id: eventId,
      created_at: createdAt,
      body,
      event_hash: `sha256:${eventId}`,
      message_id: messageId,
      stream_id: streamId,
      state: oc.state,
      ...(oc.state === 'rejected' && oc.errorCode !== null ? { error_code: oc.errorCode } : {}),
    })
  }

  return { byStream, outbox }
}

/**
 * Split a stream's ascending events into contiguous WINDOWS of size `w` delivered
 * NEWEST-FIRST — the real client shape (cold-start pulls the newest page, then
 * `sync.backfill` walks older pages backward, each calling the projection seam).
 * Each window is applied ascending (in-order within a page) but the newest window
 * lands before the older backfill pages, so a recent reply/edit/delete of an OLD
 * message is applied BEFORE its target's backfilled `message.created`.
 */
function windowsNewestFirst(events: readonly EventRow[], w: number): EventRow[][] {
  const asc = [...events].sort((a, b) => a.server_sequence - b.server_sequence)
  const chunks: EventRow[][] = []
  for (let i = 0; i < asc.length; i += w) chunks.push(asc.slice(i, i + w))
  return chunks.reverse() // newest window first, then older backfill pages
}

/**
 * Reproduce the INCREMENTAL state. `order`:
 *   • 'sorted'   — one ascending batch per stream (the in-order live/catch-up path);
 *   • 'windowed' — newest-window-first-then-backfill (the REAL cold-start + backfill
 *     ordering, exercising out-of-order cross-message references).
 * Then the outbox overlay is applied on top (created_at order), settled base first.
 */
async function buildIncremental(
  db: MsgDb,
  h: History,
  order: 'sorted' | 'windowed' = 'sorted',
): Promise<void> {
  for (const [streamId, events] of h.byStream) {
    if (events.length === 0) continue
    if (order === 'sorted') {
      await db.putEvents(events)
      await applyEventsToProjection(db, streamId, events)
    } else {
      // Persist each window to the `events` cache BEFORE applying it, so the
      // out-of-order reconcile (which scans the cache) sees earlier-delivered
      // edits/deletes exactly as the real backfill path leaves them.
      for (const window of windowsNewestFirst(events, 3)) {
        await db.putEvents(window)
        await applyEventsToProjection(db, streamId, window)
      }
    }
  }
  if (h.outbox.length > 0) await db.putOutbox(h.outbox)
  const ordered = [...h.outbox].sort((a, b) => a.created_at - b.created_at)
  for (const row of ordered) {
    if (await db.hasEvent(row.event_id)) continue // orphan: already settled — no overlay
    await applyPendingOutboxRow(db, row)
  }
}

/**
 * Rebuild through the REAL `rebuildProjections`. Under an env-gated teeth flag a
 * rebuild-pass-only bug is injected (the ENG-61 pattern) so the SAME
 * `rebuild === incremental` gate goes red; the patch is reverted immediately so
 * the next iteration's incremental pass is clean.
 */
async function rebuildMaybeSkewed(db: MsgDb): Promise<void> {
  const restore: Array<() => void> = []
  if (MUTATION === 'inv6-rebuild-skew') {
    let corrupted = false
    HANDLERS['message.created@1'] = (event, body) => {
      const row = applyMessageCreatedV1(event, body)
      if (row && !corrupted) {
        corrupted = true
        return { ...row, text: row.text + 'X' }
      }
      return row
    }
    restore.push(() => (HANDLERS['message.created@1'] = applyMessageCreatedV1))
  } else if (MUTATION === 'inv6-delete-skew') {
    // Rebuild "forgets" the tombstone: strip `deleted` on every persisted row.
    const real = db.putMessages.bind(db)
    db.putMessages = (rows: readonly MessageRow[]): Promise<void> =>
      real(rows.map((r) => ({ ...r, deleted: false })))
    restore.push(() => (db.putMessages = real))
  } else if (MUTATION === 'inv6-reaction-skew') {
    // Rebuild ignores reaction REMOVES (drops the tombstone write), so a removed
    // reaction wrongly stays present — the out-of-order LWW divergence class.
    const real = db.putReactions.bind(db)
    db.putReactions = (rows: readonly ReactionRow[]): Promise<void> =>
      real(rows.filter((r) => r.present))
    restore.push(() => (db.putReactions = real))
  } else if (MUTATION === 'inv6-file-skew') {
    // ENG-120: rebuild corrupts one file row's `name` — a files-set divergence.
    let corrupted = false
    const real = db.putFiles.bind(db)
    db.putFiles = (rows: readonly FileRow[]): Promise<void> =>
      real(
        rows.map((r) => {
          if (!corrupted) {
            corrupted = true
            return { ...r, name: r.name + 'X' }
          }
          return r
        }),
      )
    restore.push(() => (db.putFiles = real))
  }
  try {
    await rebuildProjections(db)
  } finally {
    for (const r of restore) r()
  }
}

// ===========================================================================
// Property 1 — rebuild ≡ incremental for EVERY randomized history (MemoryDb).
// ===========================================================================

describe('§12 invariant 6 — client rebuild ≡ incremental [property]', () => {
  it('drop+replay is byte-identical to incremental for any generated history (MemoryDb)', async () => {
    await fc.assert(
      fc.asyncProperty(historyArb, async (draw) => {
        const db: MsgDb = new MemoryDb()
        const h = materialize(draw)
        await buildIncremental(db, h)
        const incremental = await dumpProjection(db)

        await db.clearDerivedTables()
        await rebuildMaybeSkewed(db)
        const rebuilt = await dumpProjection(db)

        expect(rebuilt).toBe(incremental) // the gate (goes red under any teeth flag)
        await db.close()
      }),
      { numRuns: 120 },
    )
  })

  // ------------------------------------------------------------------------
  // Property 2 — the SHIPPING db: replay drawn histories through the real
  // DexieDb (fake-indexeddb). This is the gating assertion for the browser path.
  // ------------------------------------------------------------------------
  it('drop+replay is byte-identical against the real DexieDb (shipping path)', async () => {
    await fc.assert(
      fc.asyncProperty(historyArb, async (draw) => {
        const db: MsgDb = await openDb(fakeIdbOptions())
        expect(db.persistence).toBe('persistent') // real Dexie, not the MemoryDb fallback
        const h = materialize(draw)
        await buildIncremental(db, h)
        const incremental = await dumpProjection(db)

        await db.clearDerivedTables()
        await rebuildMaybeSkewed(db)
        const rebuilt = await dumpProjection(db)

        expect(rebuilt).toBe(incremental) // the gate (goes red under any teeth flag)
        await db.close()
      }),
      { numRuns: 20 },
    )
  })

  // ------------------------------------------------------------------------
  // Property 2b — REALISTIC OUT-OF-ORDER DELIVERY. The client does NOT receive
  // events in server order: cold-start pulls the newest window first, then
  // backfills older pages (sync.ts §7/§10). This gate applies each stream's events
  // NEWEST-WINDOW-FIRST-THEN-BACKFILL and asserts the resulting projection STILL
  // equals the in-order rebuild — the permanent protection against a divergence
  // that appears only under the real ordering (a recent reply/edit/delete of an old
  // message applied before its backfilled create). Both MemoryDb + real DexieDb.
  // ------------------------------------------------------------------------
  it('windowed (newest-first + backfill) delivery still equals the in-order rebuild', async () => {
    await fc.assert(
      fc.asyncProperty(historyArb, fc.boolean(), async (draw, useDexie) => {
        const db: MsgDb = useDexie ? await openDb(fakeIdbOptions()) : new MemoryDb()
        const h = materialize(draw)
        await buildIncremental(db, h, 'windowed') // OUT-OF-ORDER delivery
        const outOfOrder = await dumpProjection(db)

        await db.clearDerivedTables()
        await rebuildProjections(db) // in-order replay of the cached events
        const rebuilt = await dumpProjection(db)

        // Out-of-order incremental converges to the same projection as the
        // in-order rebuild — the whole point of the recompute-self + replay fix.
        expect(rebuilt).toBe(outOfOrder)
        await db.close()
      }),
      { numRuns: 300 },
    )
  })

  // ------------------------------------------------------------------------
  // Property 2c — DETERMINISTIC out-of-order TEETH. Proves the windowed gate above
  // has real teeth for the reply-before-root class: a reply is delivered BEFORE its
  // root; recompute-self on the root's (backfilled) create is what makes reply_count
  // converge. Defeating it (listRepliesByRoot → [] during the out-of-order pass, so
  // the root's create cannot find its already-applied reply) makes the out-of-order
  // projection diverge from the in-order rebuild → RED. The clean pass is GREEN.
  // ------------------------------------------------------------------------
  it('TEETH: reply-before-root without a working recompute-self → out-of-order gate RED', async () => {
    // Root at the OLDEST seq (backfilled last), its reply at a NEWER seq (window 1).
    const events: EventRow[] = [
      messageCreatedEvent({ streamId: 's_0', seq: 1, messageId: 'm_root', text: 'root' }),
      messageCreatedEvent({
        streamId: 's_0',
        seq: 2,
        messageId: 'm_r1',
        text: 'reply',
        threadRootId: 'm_root',
        authorUserId: 'u_a',
      }),
    ]
    // Deliver newest-first: [reply(seq2)] then [root(seq1)] → reply-before-root.
    const deliver = async (db: MsgDb, defeatRecomputeSelf: boolean): Promise<string> => {
      for (const window of [[events[1]!], [events[0]!]]) {
        await db.putEvents(window)
        if (defeatRecomputeSelf) {
          const real = db.listRepliesByRoot.bind(db)
          db.listRepliesByRoot = (): Promise<MessageRow[]> => Promise.resolve([])
          await applyEventsToProjection(db, 's_0', window)
          db.listRepliesByRoot = real
        } else {
          await applyEventsToProjection(db, 's_0', window)
        }
      }
      return dumpProjection(db)
    }

    // The in-order rebuild truth: reply_count = 1 on m_root.
    const truthDb = new MemoryDb()
    await truthDb.putEvents(events)
    await applyEventsToProjection(truthDb, 's_0', events)
    const inOrder = await dumpProjection(truthDb)
    expect(inOrder).toContain('"reply_count":1')
    await truthDb.close()

    // Positive control: clean windowed delivery converges to the in-order truth.
    const cleanDb = new MemoryDb()
    expect(await deliver(cleanDb, false)).toBe(inOrder)
    await cleanDb.close()

    // TOOTH: defeat recompute-self on the out-of-order pass → reply_count stays 0.
    const toothDb = new MemoryDb()
    const skewed = await deliver(toothDb, true)
    expect(skewed).not.toBe(inOrder) // reply-before-root did NOT converge → RED
    expect(skewed).toContain('"reply_count":0')
    await toothDb.close()
  })

  // ------------------------------------------------------------------------
  // Property 2d — DETERMINISTIC reaction-out-of-order TEETH. A reaction removed@lo
  // then added@hi delivered NEWEST-FIRST (add applied before the lower-seq remove).
  // With seq-aware LWW the lower-seq remove is skipped → present (== in-order). The
  // OLD bare-membership behaviour (last-APPLIED wins) is modelled by defeating the
  // LWW lookup (getReaction → undefined) so the lower-seq remove wrongly wins →
  // ABSENT → diverges from the in-order rebuild → RED.
  // ------------------------------------------------------------------------
  it('TEETH: reaction removed@lo + added@hi without seq-aware LWW → out-of-order gate RED', async () => {
    // In-order truth: added@4 is the highest-seq event → the reaction is PRESENT.
    const events: EventRow[] = [
      messageCreatedEvent({ streamId: 's_0', seq: 1, messageId: 'm_1', text: 'hi' }),
      reactionRemovedEvent({
        streamId: 's_0',
        seq: 3,
        messageId: 'm_1',
        emoji: '👍',
        authorUserId: 'u_a',
      }),
      reactionAddedEvent({
        streamId: 's_0',
        seq: 4,
        messageId: 'm_1',
        emoji: '👍',
        authorUserId: 'u_a',
      }),
    ]
    const truth = new MemoryDb()
    await truth.putEvents(events)
    await applyEventsToProjection(truth, 's_0', events)
    const inOrder = await dumpProjection(truth)
    expect(inOrder).toContain('👍') // present (highest-seq event is the add)
    await truth.close()

    // Deliver NEWEST-FIRST: [added@4] then [removed@3, create@1] (add before remove).
    const deliver = async (db: MsgDb, defeatLww: boolean): Promise<string> => {
      for (const window of [[events[2]!], [events[1]!, events[0]!]]) {
        await db.putEvents(window)
        if (defeatLww) {
          const real = db.getReaction.bind(db)
          db.getReaction = (): Promise<undefined> => Promise.resolve(undefined) // no LWW guard
          await applyEventsToProjection(db, 's_0', window)
          db.getReaction = real
        } else {
          await applyEventsToProjection(db, 's_0', window)
        }
      }
      return dumpProjection(db)
    }

    // Positive control: seq-aware LWW converges to the in-order truth (👍 present).
    const cleanDb = new MemoryDb()
    expect(await deliver(cleanDb, false)).toBe(inOrder)
    await cleanDb.close()

    // TOOTH: bare last-applied-wins → the lower-seq remove wins → 👍 wrongly ABSENT.
    const toothDb = new MemoryDb()
    const skewed = await deliver(toothDb, true)
    expect(skewed).not.toBe(inOrder) // reaction did NOT converge → RED
    expect(skewed).not.toContain('👍')
    await toothDb.close()
  })

  // ------------------------------------------------------------------------
  // Property 2e — DETERMINISTIC edit-before-create TEETH (matches the thread tooth
  // for the replayCachedMutations path). A recent edit of an OLD message: the edit
  // (window 1) lands before its backfilled create (window 2). With replayCachedMutations
  // the create folds in the cached edit → edited text (== in-order). Defeating the
  // cache scan (getEventsForStream → []) loses the edit → original text → RED.
  // ------------------------------------------------------------------------
  it('TEETH: edit-before-create without replayCachedMutations → out-of-order gate RED', async () => {
    const events: EventRow[] = [
      messageCreatedEvent({ streamId: 's_0', seq: 1, messageId: 'm_1', text: 'original' }),
      messageEditedEvent({ streamId: 's_0', seq: 2, messageId: 'm_1', text: 'EDITED' }),
    ]
    const truth = new MemoryDb()
    await truth.putEvents(events)
    await applyEventsToProjection(truth, 's_0', events)
    const inOrder = await dumpProjection(truth)
    expect(inOrder).toContain('EDITED')
    await truth.close()

    // Deliver NEWEST-FIRST: [edit@2] then [create@1] — edit before backfilled create.
    const deliver = async (db: MsgDb, defeatReplay: boolean): Promise<string> => {
      for (const window of [[events[1]!], [events[0]!]]) {
        await db.putEvents(window)
        if (defeatReplay) {
          const real = db.getEventsForStream.bind(db)
          db.getEventsForStream = (): Promise<EventRow[]> => Promise.resolve([]) // no cache scan
          await applyEventsToProjection(db, 's_0', window)
          db.getEventsForStream = real
        } else {
          await applyEventsToProjection(db, 's_0', window)
        }
      }
      return dumpProjection(db)
    }

    const cleanDb = new MemoryDb()
    expect(await deliver(cleanDb, false)).toBe(inOrder) // replay folds in the edit
    await cleanDb.close()

    const toothDb = new MemoryDb()
    const skewed = await deliver(toothDb, true)
    expect(skewed).not.toBe(inOrder) // edit lost → original text → RED
    expect(skewed).toContain('original')
    await toothDb.close()
  })

  // ------------------------------------------------------------------------
  // Property 2f — DETERMINISTIC file.uploaded TEETH (ENG-120). Two file.uploaded
  // for the SAME file_id (a duplicate delivery) at seq 1 and seq 3, with a
  // message.created referencing it at seq 2. Because file.uploaded is an IMMUTABLE
  // KEYED UPSERT, the two writes are byte-identical: a duplicate does not change
  // the dump AND arrival-before/after the message.created (teeth (a) + (b)) lands
  // the same final row, so windowed (newest-first) delivery converges to the
  // in-order rebuild. Modelling an ORDER-DEPENDENT / non-idempotent handler (bake
  // the event's seq into `name`) makes last-applied-wins → windowed (seq1 last) !=
  // in-order (seq3 last) → RED. The clean pass is GREEN.
  // ------------------------------------------------------------------------
  it('TEETH: a non-idempotent/order-dependent file.uploaded handler → out-of-order gate RED', async () => {
    const F = fileId(7)
    // Same file_id uploaded twice (duplicate), with a referencing create between.
    const events: EventRow[] = [
      fileUploadedEvent({ streamId: 's_0', seq: 1, fileId: F, name: 'pic.png' }),
      messageCreatedEvent({ streamId: 's_0', seq: 2, messageId: 'm_1', text: 'hi', fileIds: [F] }),
      fileUploadedEvent({ streamId: 's_0', seq: 3, fileId: F, name: 'pic.png' }),
    ]

    // In-order truth: exactly ONE files row (the duplicate is an idempotent no-op).
    const truth = new MemoryDb()
    await truth.putEvents(events)
    await applyEventsToProjection(truth, 's_0', events)
    const inOrder = await dumpProjection(truth)
    expect(await truth.count('files')).toBe(1)
    await truth.close()

    // Deliver NEWEST-FIRST: [fu@3] then [create@2, fu@1] — duplicate before create.
    const deliver = async (db: MsgDb, defeatIdempotence: boolean): Promise<string> => {
      const real = FILE_HANDLERS['file.uploaded@1']!
      if (defeatIdempotence) {
        // Order-dependent handler: stamp the event seq into the row so the two
        // deliveries are NO LONGER byte-identical (last applied wins).
        FILE_HANDLERS['file.uploaded@1'] = (event, body) => {
          const row = applyFileUploadedV1(event, body)
          return row ? { ...row, name: `${row.name}#${event.server_sequence}` } : null
        }
      }
      for (const window of [[events[2]!], [events[1]!, events[0]!]]) {
        await db.putEvents(window)
        await applyEventsToProjection(db, 's_0', window)
      }
      FILE_HANDLERS['file.uploaded@1'] = real
      return dumpProjection(db)
    }

    // Positive control: the real (idempotent) handler converges to the in-order truth.
    const cleanDb = new MemoryDb()
    expect(await deliver(cleanDb, false)).toBe(inOrder)
    await cleanDb.close()

    // TOOTH: order-dependent handler → seq1 wins under newest-first → diverges → RED.
    const toothDb = new MemoryDb()
    const skewed = await deliver(toothDb, true)
    expect(skewed).not.toBe(inOrder)
    await toothDb.close()
  })

  // ------------------------------------------------------------------------
  // Property 3 — idempotence: re-applying the whole history (settled base THEN
  // the pending overlay — the same "f then g" the rebuild does) leaves the dump
  // unchanged. Re-applying settled events alone would clobber the overlay back to
  // its settled base; re-deriving the overlay on top restores it — exactly why
  // rebuild ≡ incremental holds and the two agree by construction.
  // ------------------------------------------------------------------------
  it('re-applying the full history is a no-op on the dump (idempotence)', async () => {
    await fc.assert(
      fc.asyncProperty(historyArb, async (draw) => {
        const db: MsgDb = new MemoryDb()
        const h = materialize(draw)
        await buildIncremental(db, h)
        const before = await dumpProjection(db)
        await buildIncremental(db, h) // re-apply settled base + re-derive overlay
        expect(await dumpProjection(db)).toBe(before)
        // The shipping dump is unchanged too (never regressed by the gate dump).
        expect(typeof (await dumpMessages(db))).toBe('string')
        await db.close()
      }),
      { numRuns: 40 },
    )
  })

  // ------------------------------------------------------------------------
  // Property 4 — DETERMINISTIC M3 teeth. The env-gated property teeth
  // (inv6-delete-skew / inv6-reaction-skew) only bite when the random sample
  // happens to draw the triggering shape (a reaction add+remove, a delete). This
  // fixed history GUARANTEES both, and injects each M3 rebuild bug inline (no env
  // var) so the teeth ALWAYS bite: a positive control (clean rebuild == incremental)
  // plus a reaction-remove-ignored rebuild and a delete-not-tombstoned rebuild,
  // each of which MUST make the dumps differ.
  // ------------------------------------------------------------------------
  it('TEETH: a one-sided M3 rebuild bug (skip delete-redact / ignore reaction removes) → RED', async () => {
    // Fixed history in one stream: create m_root, a reply m_r (u_a), a reaction
    // added then removed (net: no membership), and the reply deleted.
    const events: EventRow[] = [
      messageCreatedEvent({ streamId: 's_0', seq: 1, messageId: 'm_root', text: 'root' }),
      messageCreatedEvent({
        streamId: 's_0',
        seq: 2,
        messageId: 'm_r',
        text: 'reply',
        threadRootId: 'm_root',
        authorUserId: 'u_a',
      }),
      reactionAddedEvent({
        streamId: 's_0',
        seq: 3,
        messageId: 'm_root',
        emoji: '👍',
        authorUserId: 'u_a',
      }),
      reactionRemovedEvent({
        streamId: 's_0',
        seq: 4,
        messageId: 'm_root',
        emoji: '👍',
        authorUserId: 'u_a',
      }),
      messageDeletedEvent({ streamId: 's_0', seq: 5, messageId: 'm_r' }),
    ]
    const build = async (db: MsgDb): Promise<void> => {
      await db.putEvents(events)
      await applyEventsToProjection(db, 's_0', events)
    }

    // Positive control: a CLEAN rebuild is byte-identical (so the != below has meaning).
    const clean = new MemoryDb()
    await build(clean)
    const incremental = await dumpProjection(clean)
    await clean.clearDerivedTables()
    await rebuildProjections(clean)
    expect(await dumpProjection(clean)).toBe(incremental)
    await clean.close()

    // TOOTH 1 — rebuild IGNORES reaction removes (drops the tombstone write), so
    // the removed 👍 wrongly stays present.
    const t1 = new MemoryDb()
    await build(t1)
    await t1.clearDerivedTables()
    const realPutReactions = t1.putReactions.bind(t1)
    t1.putReactions = (rows: readonly ReactionRow[]): Promise<void> =>
      realPutReactions(rows.filter((r) => r.present))
    await rebuildProjections(t1)
    t1.putReactions = realPutReactions
    expect(await dumpProjection(t1)).not.toBe(incremental) // stale 👍 survives
    await t1.close()

    // TOOTH 2 — rebuild SKIPS the delete tombstone: strip `deleted` on write.
    const t2 = new MemoryDb()
    await build(t2)
    await t2.clearDerivedTables()
    const realPut = t2.putMessages.bind(t2)
    t2.putMessages = (rows): Promise<void> => realPut(rows.map((r) => ({ ...r, deleted: false })))
    await rebuildProjections(t2)
    t2.putMessages = realPut
    // m_r is not tombstoned on rebuild → its row + the root's reply_count differ.
    expect(await dumpProjection(t2)).not.toBe(incremental)
    await t2.close()
  })
})
