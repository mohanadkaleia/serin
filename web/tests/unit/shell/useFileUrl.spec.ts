// tests/unit/shell/useFileUrl.spec.ts — the ENG-119 object-URL seam (which ENG-121
// builds directly on). Covers the non-trivial refcounted-sharing + revoke lifecycle:
// concurrent mounts share one URL + one download, the last unmount revokes exactly
// once, an unmount MID-fetch still revokes on resolve without a use-after-revoke or
// leak, and a fresh mount after the entry was deleted uses a NEW URL that a late
// revoke of the old one cannot clobber. `URL.createObjectURL`/`revokeObjectURL` are
// spied; the worker client's `files.download`/`thumbnail` are faked.

import { effectScope } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFileUrl } from '../../../src/composables/useFileUrl'
import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import type { FileFetchResult, WorkerClient } from '../../../src/worker'

let counter = 0
let createSpy: ReturnType<typeof vi.fn>
let revokeSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  counter = 0
  createSpy = vi.fn(() => `blob:mock-${++counter}`)
  revokeSpy = vi.fn()
  // jsdom implements neither; we install our own spies as own properties.
  URL.createObjectURL = createSpy
  URL.revokeObjectURL = revokeSpy
})

afterEach(() => {
  setWorkerClient(undefined)
  // Remove the stubs so they don't leak into other suites (jsdom had none).
  delete (URL as { createObjectURL?: unknown }).createObjectURL
  delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL
})

/** A minimal WorkerClient exposing only the `files.download`/`thumbnail` seams. */
function fakeClient(download: (fileId: string) => Promise<FileFetchResult>): WorkerClient {
  return {
    files: {
      download,
      thumbnail: download,
      upload: () => Promise.resolve({ upload_id: 'x' }),
      retry: () => Promise.resolve({ upload_id: 'x' }),
      cancel: () => Promise.resolve({ upload_id: 'x' }),
      onProgress: () => () => {},
    },
  } as unknown as WorkerClient
}

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

/** Drain the microtask/macrotask queue so the fetch + URL chains settle. */
const flushAsync = async (n = 6): Promise<void> => {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0))
}

describe('useFileUrl — refcounted object-URL sharing', () => {
  it('shares one URL + one download across concurrent mounts; revokes once on last unmount', async () => {
    const download = vi.fn(() => Promise.resolve({ blob: new Blob(['x']) }))
    setWorkerClient(fakeClient(download))

    const s1 = effectScope()
    let h1!: ReturnType<typeof useFileUrl>
    s1.run(() => {
      h1 = useFileUrl('f_share')
    })
    const s2 = effectScope()
    let h2!: ReturnType<typeof useFileUrl>
    s2.run(() => {
      h2 = useFileUrl('f_share')
    })
    await flushAsync()

    expect(download).toHaveBeenCalledTimes(1) // one fetch, shared
    expect(createSpy).toHaveBeenCalledTimes(1) // one object URL
    expect(h1.url.value).not.toBeNull()
    expect(h1.url.value).toBe(h2.url.value) // both refs see the same URL

    s1.stop() // refcount 2 → 1: no revoke yet
    await flushAsync()
    expect(revokeSpy).not.toHaveBeenCalled()

    s2.stop() // refcount 1 → 0: revoke exactly once
    await flushAsync()
    expect(revokeSpy).toHaveBeenCalledTimes(1)
    expect(revokeSpy).toHaveBeenCalledWith(h1.url.value)
  })

  it('unmounting mid-fetch revokes on resolve — no leak, no use-after-revoke', async () => {
    const d = deferred<FileFetchResult>()
    const download = vi.fn(() => d.promise)
    setWorkerClient(fakeClient(download))

    const s = effectScope()
    s.run(() => {
      useFileUrl('f_mid')
    })
    await flushAsync()
    expect(createSpy).not.toHaveBeenCalled() // still in flight

    s.stop() // unmount BEFORE the fetch resolves → refcount 0, entry deleted
    await flushAsync()
    d.resolve({ blob: new Blob(['x']) }) // the fetch now lands
    await flushAsync()

    // The URL was created then revoked exactly once — scheduled on resolve, no leak.
    expect(createSpy).toHaveBeenCalledTimes(1)
    expect(revokeSpy).toHaveBeenCalledTimes(1)
    expect(revokeSpy).toHaveBeenCalledWith(createSpy.mock.results[0]?.value)
  })

  it('a fresh mount after the entry was deleted uses a NEW URL a late revoke cannot clobber', async () => {
    const download = vi.fn(() => Promise.resolve({ blob: new Blob(['x']) }))
    setWorkerClient(fakeClient(download))

    const s1 = effectScope()
    let h1!: ReturnType<typeof useFileUrl>
    s1.run(() => {
      h1 = useFileUrl('f_fresh')
    })
    await flushAsync()
    const url1 = h1.url.value
    s1.stop() // delete the entry + revoke url1
    await flushAsync()
    expect(revokeSpy).toHaveBeenCalledTimes(1)
    expect(revokeSpy).toHaveBeenCalledWith(url1)

    const s2 = effectScope()
    let h2!: ReturnType<typeof useFileUrl>
    s2.run(() => {
      h2 = useFileUrl('f_fresh') // fresh entry — the old one was deleted
    })
    await flushAsync()
    const url2 = h2.url.value

    expect(download).toHaveBeenCalledTimes(2) // a new fetch, not the dead entry's
    expect(createSpy).toHaveBeenCalledTimes(2)
    expect(url2).not.toBe(url1)
    // The earlier revoke targeted url1 ONLY — the new URL is untouched.
    expect(revokeSpy).toHaveBeenCalledTimes(1)
    expect(revokeSpy).not.toHaveBeenCalledWith(url2)
    s2.stop()
  })

  it('a 404 (null blob) yields a null url and no createObjectURL', async () => {
    const download = vi.fn(() => Promise.resolve({ blob: null }))
    setWorkerClient(fakeClient(download))

    const s = effectScope()
    let h!: ReturnType<typeof useFileUrl>
    s.run(() => {
      h = useFileUrl('f_404')
    })
    await flushAsync()

    expect(h.url.value).toBeNull()
    expect(createSpy).not.toHaveBeenCalled()
    s.stop()
    await flushAsync()
    expect(revokeSpy).not.toHaveBeenCalled() // nothing to revoke
  })
})
