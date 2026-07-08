// tests/unit/worker/file-projection.spec.ts — ENG-120 client `file.uploaded`
// projection: the keyed-upsert `files` set + `MessageRow.file_ids` + the
// `attachments.forMessage` query. Direct on MemoryDb + the real DexieDb
// (fake-indexeddb) so the shipping IndexedDB path is exercised too.
//
// Unlike the ENG-100 stateful handlers, `file.uploaded` is an IMMUTABLE keyed
// upsert, so order-independence is FREE: these tests prove the row is byte-stable
// under duplicates AND that a file.uploaded landing before OR after its referencing
// message.created converges to the same attachments result. The rebuild ≡
// incremental property (incl. `dumpFiles`) lives in the invariant-6 property gate.

import { describe, expect, it, vi } from 'vitest'

import { MemoryDb, openDb } from '../../../src/worker/db'
import {
  applyEventsToProjection,
  applyFileUploadedV1,
  dumpFiles,
  FILE_HANDLERS,
  listAttachments,
} from '../../../src/worker/projection'
import type { EventRow, MsgDb } from '../../../src/worker/types'

import { fakeIdbOptions } from './helpers'
import { fileId, fileUploadedEvent, messageCreatedEvent } from './projfixtures'

const S = 's_files'
const F1 = fileId(1)
const F2 = fileId(2)

/** Apply a per-stream list of events (single batch) through the real seam. */
async function apply(db: MsgDb, events: readonly EventRow[]): Promise<void> {
  await db.putEvents([...events])
  await applyEventsToProjection(db, S, events)
}

// ===========================================================================
// applyFileUploadedV1 — the pure row builder (unit, no db).
// ===========================================================================

describe('applyFileUploadedV1 (pure row builder)', () => {
  it('builds a FileRow from the payload + the envelope stream_id', () => {
    const ev = fileUploadedEvent({
      streamId: S,
      seq: 1,
      fileId: F1,
      sha256: 'b'.repeat(64),
      name: 'résumé.pdf',
      mimeType: 'application/pdf',
      sizeBytes: 4096,
    })
    const row = applyFileUploadedV1(ev, ev.envelope!.body)
    expect(row).toEqual({
      file_id: F1,
      sha256: 'b'.repeat(64),
      name: 'résumé.pdf',
      mime_type: 'application/pdf',
      size_bytes: 4096,
      stream_id: S, // from the ENVELOPE, NOT the payload (which carries no stream_id)
    })
  })

  it('is registered under file.uploaded@1 in FILE_HANDLERS', () => {
    expect(FILE_HANDLERS['file.uploaded@1']).toBe(applyFileUploadedV1)
  })

  describe('D9: skip (warn, never throw) on a malformed-known payload', () => {
    it.each([
      [
        'non-object payload',
        { streamId: S, seq: 1, fileId: F1 },
        (b: Record<string, unknown>) => (b.payload = null),
      ],
      [
        'missing file_id',
        { streamId: S, seq: 1, fileId: F1 },
        (b: Record<string, unknown>) => delete (b.payload as Record<string, unknown>).file_id,
      ],
      [
        'non-f_ file_id',
        { streamId: S, seq: 1, fileId: F1 },
        (b: Record<string, unknown>) =>
          ((b.payload as Record<string, unknown>).file_id = 'm_00000000000000000000000001'),
      ],
      [
        'non-string file_id',
        { streamId: S, seq: 1, fileId: F1 },
        (b: Record<string, unknown>) => ((b.payload as Record<string, unknown>).file_id = 42),
      ],
      [
        'missing sha256',
        { streamId: S, seq: 1, fileId: F1 },
        (b: Record<string, unknown>) => delete (b.payload as Record<string, unknown>).sha256,
      ],
      [
        'non-number size_bytes',
        { streamId: S, seq: 1, fileId: F1 },
        (b: Record<string, unknown>) => ((b.payload as Record<string, unknown>).size_bytes = '10'),
      ],
    ])('%s → null', (_label, opts, mutate) => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
      const ev = fileUploadedEvent(opts)
      mutate(ev.envelope!.body)
      expect(() => applyFileUploadedV1(ev, ev.envelope!.body)).not.toThrow()
      expect(applyFileUploadedV1(ev, ev.envelope!.body)).toBeNull()
      expect(warn).toHaveBeenCalled()
      warn.mockRestore()
    })
  })
})

// ===========================================================================
// applyEventsToProjection routing + the files set (against both DB backends).
// ===========================================================================

describe.each([
  { name: 'MemoryDb', make: (): Promise<MsgDb> => Promise.resolve(new MemoryDb()) },
  { name: 'DexieDb', make: (): Promise<MsgDb> => openDb(fakeIdbOptions()) },
])('file.uploaded projection [$name]', ({ make }) => {
  it('routes file.uploaded@1 → a files row keyed by file_id', async () => {
    const db = await make()
    await apply(db, [fileUploadedEvent({ streamId: S, seq: 1, fileId: F1 })])
    const row = await db.getFile(F1)
    expect(row?.file_id).toBe(F1)
    expect(row?.stream_id).toBe(S)
    expect(await db.count('files')).toBe(1)
    await db.close()
  })

  it('is an idempotent keyed upsert: a duplicate delivery leaves the dump unchanged', async () => {
    const db = await make()
    await apply(db, [fileUploadedEvent({ streamId: S, seq: 1, fileId: F1 })])
    const before = await dumpFiles(db)
    // Re-deliver the SAME event (a duplicate) — byte-identical row, no-op on the dump.
    await apply(db, [fileUploadedEvent({ streamId: S, seq: 2, fileId: F1 })])
    expect(await dumpFiles(db)).toBe(before)
    expect(await db.count('files')).toBe(1)
    await db.close()
  })

  it('skips an above-max version (file.uploaded@2) — D9, files stays empty', async () => {
    const db = await make()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    await apply(db, [fileUploadedEvent({ streamId: S, seq: 1, fileId: F1, typeVersion: 2 })])
    expect(await db.count('files')).toBe(0)
    warn.mockRestore()
    await db.close()
  })

  it('dumpFiles is deterministic (sorted by file_id, stable field order)', async () => {
    const db = await make()
    // Apply out of file_id order; the dump must sort ascending by file_id.
    await apply(db, [
      fileUploadedEvent({ streamId: S, seq: 1, fileId: F2, name: 'two.png' }),
      fileUploadedEvent({ streamId: S, seq: 2, fileId: F1, name: 'one.png' }),
    ])
    const dump = await dumpFiles(db)
    const lines = dump.split('\n')
    expect(lines[0]).toBe(
      JSON.stringify({
        file_id: F1,
        stream_id: S,
        sha256: 'a'.repeat(64),
        name: 'one.png',
        mime_type: 'image/png',
        size_bytes: 1234,
      }),
    )
    expect(lines[1]).toContain(F2)
    await db.close()
  })
})

// ===========================================================================
// Order-independence + the attachments query (against both DB backends).
// ===========================================================================

describe.each([
  { name: 'MemoryDb', make: (): Promise<MsgDb> => Promise.resolve(new MemoryDb()) },
  { name: 'DexieDb', make: (): Promise<MsgDb> => openDb(fakeIdbOptions()) },
])('attachments.forMessage query + order-independence [$name]', ({ make }) => {
  const M = 'm_att'

  it('projects file_ids onto the message row (verbatim from the body)', async () => {
    const db = await make()
    await apply(db, [messageCreatedEvent({ streamId: S, seq: 1, messageId: M, fileIds: [F1, F2] })])
    expect((await db.getMessage(M))?.file_ids).toEqual([F1, F2])
    await db.close()
  })

  it('resolves present file_ids to FileRows, in message order', async () => {
    const db = await make()
    await apply(db, [
      messageCreatedEvent({ streamId: S, seq: 1, messageId: M, fileIds: [F2, F1] }),
      fileUploadedEvent({ streamId: S, seq: 2, fileId: F1 }),
      fileUploadedEvent({ streamId: S, seq: 3, fileId: F2 }),
    ])
    const res = await listAttachments(db, M)
    expect(res.files.map((f) => f.file_id)).toEqual([F2, F1]) // message-order, not upload-order
    expect(res.pending_file_ids).toEqual([])
    await db.close()
  })

  it('marks a not-yet-projected file_id as pending (omitted from files)', async () => {
    const db = await make()
    await apply(db, [
      messageCreatedEvent({ streamId: S, seq: 1, messageId: M, fileIds: [F1, F2] }),
      fileUploadedEvent({ streamId: S, seq: 2, fileId: F1 }), // F2 never uploaded
    ])
    const res = await listAttachments(db, M)
    expect(res.files.map((f) => f.file_id)).toEqual([F1])
    expect(res.pending_file_ids).toEqual([F2])
    await db.close()
  })

  it('empty file_ids (and an unknown message) → empty result', async () => {
    const db = await make()
    await apply(db, [messageCreatedEvent({ streamId: S, seq: 1, messageId: M })])
    expect(await listAttachments(db, M)).toEqual({ message_id: M, files: [], pending_file_ids: [] })
    expect(await listAttachments(db, 'm_missing')).toEqual({
      message_id: 'm_missing',
      files: [],
      pending_file_ids: [],
    })
    await db.close()
  })

  it('converges regardless of order: file.uploaded BEFORE vs AFTER its message.created', async () => {
    // file.uploaded FIRST (arrives before the referencing create — the backfill shape).
    const before = await make()
    await apply(before, [
      fileUploadedEvent({ streamId: S, seq: 1, fileId: F1 }),
      messageCreatedEvent({ streamId: S, seq: 2, messageId: M, fileIds: [F1] }),
    ])
    const resBefore = await listAttachments(before, M)

    // file.uploaded SECOND (the in-order shape).
    const after = await make()
    await apply(after, [
      messageCreatedEvent({ streamId: S, seq: 1, messageId: M, fileIds: [F1] }),
      fileUploadedEvent({ streamId: S, seq: 2, fileId: F1 }),
    ])
    const resAfter = await listAttachments(after, M)

    // Both converge: the row's file_ids is set regardless, and the file resolves.
    expect(resBefore).toEqual(resAfter)
    expect(resBefore.files.map((f) => f.file_id)).toEqual([F1])
    expect(await dumpFiles(before)).toBe(await dumpFiles(after))
    await before.close()
    await after.close()
  })
})
