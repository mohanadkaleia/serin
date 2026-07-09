import { describe, expect, it } from 'vitest'

import { MemoryDb } from '../../../src/worker/db'
import { listDirectory } from '../../../src/worker/projection'
import type { StreamRow } from '../../../src/worker/types'

import { metaUserEvent } from './projfixtures'

const stream = (over: Partial<StreamRow> & { stream_id: string }): StreamRow => ({
  kind: 'channel',
  head_seq: 0,
  member: true,
  ...over,
})

describe('worker/projection — listDirectory (ENG-101 mention source)', () => {
  it('folds workspace-meta user events into a member list and lists channels', async () => {
    const db = new MemoryDb()
    await db.putStreams([
      stream({ stream_id: 's_meta', kind: 'workspace-meta', name: 'meta' }),
      stream({ stream_id: 's_general', name: 'general' }),
      stream({ stream_id: 's_random', name: 'random' }),
      stream({ stream_id: 's_dm', kind: 'dm', name: 'dana' }),
    ])
    await db.putEvents([
      metaUserEvent('s_meta', 1, 'user.joined', { user_id: 'u_dana', display_name: 'Dana' }),
      metaUserEvent('s_meta', 2, 'user.joined', { user_id: 'u_sam', display_name: 'Sam' }),
      metaUserEvent('s_meta', 3, 'user.joined', { user_id: 'u_gone', display_name: 'Gone' }),
      metaUserEvent('s_meta', 4, 'user.left', { user_id: 'u_gone' }),
      metaUserEvent('s_meta', 5, 'user.profile_updated', {
        user_id: 'u_sam',
        display_name: 'Samuel',
      }),
    ])

    const dir = await listDirectory(db)

    // Left users are dropped; renames apply; sorted by display name.
    expect(dir.users).toEqual([
      { user_id: 'u_dana', display_name: 'Dana' },
      { user_id: 'u_sam', display_name: 'Samuel' },
    ])
    // Channels exclude DMs + the meta stream, sorted by name.
    expect(dir.channels).toEqual([
      { stream_id: 's_general', name: 'general' },
      { stream_id: 's_random', name: 'random' },
    ])
  })

  it('excludes non-member channels and returns empty lists with no meta stream', async () => {
    const db = new MemoryDb()
    await db.putStreams([
      stream({ stream_id: 's_pub', name: 'public', member: true }),
      stream({ stream_id: 's_hidden', name: 'hidden', member: false }),
    ])

    const dir = await listDirectory(db)
    expect(dir.users).toEqual([])
    expect(dir.channels).toEqual([{ stream_id: 's_pub', name: 'public' }])
  })

  // SECURITY (PR #91 review) — defense-in-depth fold guard: a user may only rename
  // THEMSELVES, so a `user.profile_updated` whose author != subject is ignored.
  it('ignores a forged profile_updated rename (author != subject), keeps a self-rename', async () => {
    const db = new MemoryDb()
    await db.putStreams([stream({ stream_id: 's_meta', kind: 'workspace-meta', name: 'meta' })])
    await db.putEvents([
      metaUserEvent('s_meta', 1, 'user.joined', { user_id: 'u_dana', display_name: 'Dana' }),
      metaUserEvent('s_meta', 2, 'user.joined', { user_id: 'u_sam', display_name: 'Sam' }),
      // FORGED: u_sam tries to rename u_dana to "PWNED" — author (u_sam) != subject
      // (u_dana). Must be ignored: Dana keeps her name.
      metaUserEvent(
        's_meta',
        3,
        'user.profile_updated',
        { user_id: 'u_dana', display_name: 'PWNED' },
        'u_sam',
      ),
      // LEGIT: u_sam renames themselves — author == subject, so it applies.
      metaUserEvent('s_meta', 4, 'user.profile_updated', {
        user_id: 'u_sam',
        display_name: 'Samuel',
      }),
    ])

    const dir = await listDirectory(db)

    expect(dir.users).toEqual([
      { user_id: 'u_dana', display_name: 'Dana' },
      { user_id: 'u_sam', display_name: 'Samuel' },
    ])
  })
})
