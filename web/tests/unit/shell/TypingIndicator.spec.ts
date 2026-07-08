// tests/unit/shell/TypingIndicator.spec.ts — ENG-128. The typing line subscribes
// to the worker's ephemeral `{kind:'typing', stream_id}` push for the OPEN stream
// (re-subscribing on stream change, unsubscribing on unmount — no leaks), EXCLUDES
// the signed-in user, resolves ids via the directory map (raw-id fallback), and
// words the line by count: 1 → "is typing…", 2 → "and", 3+ → "Several people".
// Proven against a fake WorkerClient whose push callback the test drives directly.
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TypingIndicator from '../../../src/components/shell/TypingIndicator.vue'
import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import type { TypingPush, WorkerClient } from '../../../src/worker'

/** A minimal fake exposing only the typing subscription seam. */
function makeFakeClient(): {
  client: WorkerClient
  subscribeSpy: ReturnType<typeof vi.fn>
  push: (streamId: string, userIds: string[]) => void
  unsubscribed: string[]
} {
  const subs = new Map<string, (p: TypingPush) => void>()
  const unsubscribed: string[] = []
  const subscribeSpy = vi.fn((streamId: string, cb: (p: TypingPush) => void) => {
    subs.set(streamId, cb)
    return () => {
      unsubscribed.push(streamId)
      subs.delete(streamId)
    }
  })
  const client = { typing: { subscribe: subscribeSpy } } as unknown as WorkerClient
  return {
    client,
    subscribeSpy,
    push: (streamId, user_ids) => subs.get(streamId)?.({ stream_id: streamId, user_ids }),
    unsubscribed,
  }
}

const NAMES: ReadonlyMap<string, string> = new Map([
  ['u_me', 'Me Myself'],
  ['u_dana', 'Dana'],
  ['u_sam', 'Sam'],
  ['u_kim', 'Kim'],
])

async function mountIndicator(fake: ReturnType<typeof makeFakeClient>, streamId = 's1') {
  setWorkerClient(fake.client)
  const wrapper = mount(TypingIndicator, {
    props: { streamId, names: NAMES, myUserId: 'u_me' },
  })
  await flushPromises() // resolve the async client + subscribe
  return wrapper
}

const textOf = (wrapper: Awaited<ReturnType<typeof mountIndicator>>) =>
  wrapper.find('[data-testid="typing-indicator"]')

describe('TypingIndicator (ENG-128)', () => {
  afterEach(() => setWorkerClient(undefined))

  it('renders nothing while nobody is typing', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    expect(fake.subscribeSpy).toHaveBeenCalledWith('s1', expect.any(Function))
    expect(textOf(wrapper).exists()).toBe(false)
  })

  it('renders "{Name} is typing…" for one typer (directory-resolved)', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    fake.push('s1', ['u_dana'])
    await flushPromises()
    expect(textOf(wrapper).text()).toBe('Dana is typing…')
  })

  it('falls back to the raw id when the directory has no name', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    fake.push('s1', ['u_ghost'])
    await flushPromises()
    expect(textOf(wrapper).text()).toBe('u_ghost is typing…')
  })

  it('renders "{A} and {B} are typing…" for two typers', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    fake.push('s1', ['u_dana', 'u_sam'])
    await flushPromises()
    expect(textOf(wrapper).text()).toBe('Dana and Sam are typing…')
  })

  it('renders "Several people are typing…" for three or more', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    fake.push('s1', ['u_dana', 'u_sam', 'u_kim'])
    await flushPromises()
    expect(textOf(wrapper).text()).toBe('Several people are typing…')
  })

  it('EXCLUDES the signed-in user (my own typing is not news)', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)

    fake.push('s1', ['u_me'])
    await flushPromises()
    expect(textOf(wrapper).exists()).toBe(false)

    // Me + one other → renders as ONE typer, not two.
    fake.push('s1', ['u_me', 'u_dana'])
    await flushPromises()
    expect(textOf(wrapper).text()).toBe('Dana is typing…')
  })

  it('clears when the worker pushes the emptied (TTL-expired) set', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    fake.push('s1', ['u_dana'])
    await flushPromises()
    expect(textOf(wrapper).exists()).toBe(true)

    fake.push('s1', [])
    await flushPromises()
    expect(textOf(wrapper).exists()).toBe(false)
  })

  it('re-subscribes on stream change: old sub torn down, state reset', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    fake.push('s1', ['u_dana'])
    await flushPromises()
    expect(textOf(wrapper).exists()).toBe(true)

    await wrapper.setProps({ streamId: 's2' })
    await flushPromises()

    // The s1 subscription was released and the line cleared for the new stream.
    expect(fake.unsubscribed).toContain('s1')
    expect(fake.subscribeSpy).toHaveBeenCalledWith('s2', expect.any(Function))
    expect(textOf(wrapper).exists()).toBe(false)

    // Pushes for the NEW stream render; the old stream's callback is gone.
    fake.push('s2', ['u_sam'])
    await flushPromises()
    expect(textOf(wrapper).text()).toBe('Sam is typing…')
  })

  it('unsubscribes on unmount (no leaked callback)', async () => {
    const fake = makeFakeClient()
    const wrapper = await mountIndicator(fake)
    wrapper.unmount()
    expect(fake.unsubscribed).toContain('s1')
  })

  it('never subscribes without a stream', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    mount(TypingIndicator, { props: { streamId: null, names: NAMES, myUserId: 'u_me' } })
    await flushPromises()
    expect(fake.subscribeSpy).not.toHaveBeenCalled()
  })
})
