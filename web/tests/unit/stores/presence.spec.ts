// tests/unit/stores/presence.spec.ts — ENG-136 PR-3. The presence store is a pure
// tab-side mirror of the ENG-126 ephemeral presence snapshot: it subscribes via the
// worker client (NEVER HTTP), replaces its map whole on each `{kind:'presence'}`
// push, and resolves lookups. Others default `offline`; the signed-in user defaults
// `online` (connected ⇒ online). Proven with a fake client whose push callback the
// test drives directly.
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import { usePresenceStore } from '../../../src/stores/presence'
import type { PresenceEntry, PresencePush, WorkerClient } from '../../../src/worker'

/** A minimal fake exposing the presence subscription seam only. */
function makeFakeClient(): {
  client: WorkerClient
  push: (entries: PresenceEntry[]) => void
  unsubbed: () => boolean
  subscribeSpy: ReturnType<typeof vi.fn>
} {
  let cb: ((p: PresencePush) => void) | undefined
  let unsubscribed = false
  const subscribeSpy = vi.fn((c: (p: PresencePush) => void) => {
    cb = c
    return () => {
      unsubscribed = true
    }
  })
  const client = {
    presence: { subscribe: subscribeSpy },
  } as unknown as WorkerClient
  return {
    client,
    push: (entries) => cb?.({ presence: entries }),
    unsubbed: () => unsubscribed,
    subscribeSpy,
  }
}

describe('usePresenceStore (ENG-136 PR-3)', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => setWorkerClient(undefined))

  it('subscribes once and mirrors the snapshot map', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const store = usePresenceStore()

    await store.start('u_me')
    await store.start('u_me') // idempotent — no second subscription
    expect(fake.subscribeSpy).toHaveBeenCalledTimes(1)

    fake.push([
      { user_id: 'u_dana', status: 'online' },
      { user_id: 'u_sam', status: 'offline' },
    ])
    expect(store.statusOf('u_dana')).toBe('online')
    expect(store.statusOf('u_sam')).toBe('offline')
  })

  it('defaults unknown users to offline', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const store = usePresenceStore()
    await store.start('u_me')
    expect(store.statusOf('u_nobody')).toBe('offline')
  })

  it('reports myStatus online by default, reflecting an explicit self frame', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const store = usePresenceStore()
    await store.start('u_me')

    // No frame for me yet → online by default (connected).
    expect(store.myStatus).toBe('online')

    // An explicit offline frame for me is honored.
    fake.push([{ user_id: 'u_me', status: 'offline' }])
    expect(store.myStatus).toBe('offline')

    // Back online.
    fake.push([{ user_id: 'u_me', status: 'online' }])
    expect(store.myStatus).toBe('online')
  })

  it('stop() unsubscribes and clears the snapshot', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const store = usePresenceStore()
    await store.start('u_me')
    fake.push([{ user_id: 'u_dana', status: 'online' }])
    expect(store.statusOf('u_dana')).toBe('online')

    store.stop()
    expect(fake.unsubbed()).toBe(true)
    expect(store.statusOf('u_dana')).toBe('offline')
  })
})
