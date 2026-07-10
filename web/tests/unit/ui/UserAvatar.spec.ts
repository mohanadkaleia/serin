// tests/unit/ui/UserAvatar.spec.ts — the shared avatar atom (ENG-152). Covers the
// image-vs-initials decision: initials with no sha (ZERO worker traffic), the
// image once the worker resolves the bytes (via useAvatarUrl → `users.avatar`),
// the load-error fallback to initials, and reactivity to a sha change/clear.
// `URL.createObjectURL`/`revokeObjectURL` are stubbed (jsdom has neither).

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import UserAvatar from '../../../src/components/ui/UserAvatar.vue'
import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import type { AvatarFetchResult, WorkerClient } from '../../../src/worker'

let counter = 0
let createSpy: ReturnType<typeof vi.fn>
let revokeSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  counter = 0
  createSpy = vi.fn(() => `blob:mock-${++counter}`)
  revokeSpy = vi.fn()
  URL.createObjectURL = createSpy
  URL.revokeObjectURL = revokeSpy
})

afterEach(() => {
  setWorkerClient(undefined)
  delete (URL as { createObjectURL?: unknown }).createObjectURL
  delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL
})

/** A minimal WorkerClient exposing only the `users.avatar` seam. */
function fakeClient(
  avatar: (userId: string, sha: string) => Promise<AvatarFetchResult>,
): WorkerClient {
  return { users: { avatar } } as unknown as WorkerClient
}

// Distinct per test: the composable's refcount map is module-level, so tests must
// not share a `userId:sha` key (a prior mount's hold would leak into the next test).
let shaCounter = 0
function freshSha(): string {
  shaCounter++
  return `${shaCounter}`.padStart(64, 'f')
}

describe('UserAvatar — image when set, initials otherwise', () => {
  it('renders the initial (uppercased first letter) with no sha and calls no worker', async () => {
    const avatarSpy = vi.fn()
    setWorkerClient(fakeClient(avatarSpy))
    const wrapper = mount(UserAvatar, { props: { userId: 'u_dana', name: 'dana scully' } })
    await flushPromises()

    expect(wrapper.text()).toBe('D')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(avatarSpy).not.toHaveBeenCalled()
  })

  it("renders '?' for an empty name", () => {
    const wrapper = mount(UserAvatar, { props: { name: '   ' } })
    expect(wrapper.text()).toBe('?')
  })

  it('renders the image once the worker resolves the bytes for (userId, sha)', async () => {
    const sha = freshSha()
    const avatarSpy = vi.fn(() => Promise.resolve({ blob: new Blob([new Uint8Array(4).buffer]) }))
    setWorkerClient(fakeClient(avatarSpy))
    const wrapper = mount(UserAvatar, {
      props: { userId: 'u_dana', name: 'Dana', sha },
    })
    await flushPromises()

    expect(avatarSpy).toHaveBeenCalledWith('u_dana', sha)
    const img = wrapper.find('img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toBe('blob:mock-1')
    expect(wrapper.text()).toBe('') // no initial while the image shows
  })

  it('falls back to the initial when the fetch 404s (null blob)', async () => {
    setWorkerClient(fakeClient(() => Promise.resolve({ blob: null })))
    const wrapper = mount(UserAvatar, {
      props: { userId: 'u_dana', name: 'Dana', sha: freshSha() },
    })
    await flushPromises()

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toBe('D')
  })

  it('falls back to the initial on an <img> load error', async () => {
    setWorkerClient(
      fakeClient(() => Promise.resolve({ blob: new Blob([new Uint8Array(1).buffer]) })),
    )
    const wrapper = mount(UserAvatar, {
      props: { userId: 'u_dana', name: 'Dana', sha: freshSha() },
    })
    await flushPromises()
    expect(wrapper.find('img').exists()).toBe(true)

    await wrapper.find('img').trigger('error')

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toBe('D')
  })

  it('reacts to the sha clearing (avatar removed → initials return)', async () => {
    setWorkerClient(
      fakeClient(() => Promise.resolve({ blob: new Blob([new Uint8Array(1).buffer]) })),
    )
    const wrapper = mount(UserAvatar, {
      props: { userId: 'u_dana', name: 'Dana', sha: freshSha() },
    })
    await flushPromises()
    expect(wrapper.find('img').exists()).toBe(true)
    const src = wrapper.find('img').attributes('src')

    await wrapper.setProps({ sha: undefined })
    await flushPromises()

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toBe('D')
    // The last holder released → the object URL was revoked.
    expect(revokeSpy).toHaveBeenCalledWith(src)
  })
})
