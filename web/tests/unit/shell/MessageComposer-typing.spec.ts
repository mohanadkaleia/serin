// tests/unit/shell/MessageComposer-typing.spec.ts — ENG-128. Typing in the
// composer fires the ephemeral `client.typing.send(streamId)` signal on content
// updates: the WORKER rate-limits (~1/3s), so the UI calls it freely per update —
// but never without a real stream, and never for empty content. The existing
// send/Enter contract is untouched (covered by MessageComposer.spec.ts).
import { flushPromises, mount } from '@vue/test-utils'
import type { JSONContent } from '@tiptap/core'
import { afterEach, describe, expect, it, vi } from 'vitest'

import MessageComposer from '../../../src/components/shell/MessageComposer.vue'
import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import type { WorkerClient } from '../../../src/worker'

/** A minimal fake exposing only the typing.send seam. */
function makeFakeClient(): { client: WorkerClient; sendSpy: ReturnType<typeof vi.fn> } {
  const sendSpy = vi.fn(() => Promise.resolve({ ok: true as const }))
  const client = { typing: { send: sendSpy } } as unknown as WorkerClient
  return { client, sendSpy }
}

/** The exposed test surface (the TipTap editor's command API). */
interface ComposerVm {
  editor: { commands: { setContent: (c: JSONContent, emitUpdate?: boolean) => void } }
}

const doc = (text: string): JSONContent => ({
  type: 'doc',
  content: [{ type: 'paragraph', content: [{ type: 'text', text }] }],
})
const emptyDoc = (): JSONContent => ({ type: 'doc', content: [{ type: 'paragraph' }] })

async function mountComposer(props: Record<string, unknown> = {}) {
  const wrapper = mount(MessageComposer, { props, attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('MessageComposer typing signal (ENG-128)', () => {
  afterEach(() => {
    setWorkerClient(undefined)
    document.body.innerHTML = ''
  })

  it('calls client.typing.send(streamId) when content is typed', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const wrapper = await mountComposer({ streamId: 's1' })

    const vm = wrapper.vm as unknown as ComposerVm
    vm.editor.commands.setContent(doc('hel'), true) // emitUpdate → onUpdate fires
    await flushPromises()
    expect(fake.sendSpy).toHaveBeenCalledWith('s1')

    // Each further update signals again — throttling is the WORKER's job.
    vm.editor.commands.setContent(doc('hello'), true)
    await flushPromises()
    expect(fake.sendSpy).toHaveBeenCalledTimes(2)
  })

  it('does NOT signal without a stream', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const wrapper = await mountComposer() // no streamId

    const vm = wrapper.vm as unknown as ComposerVm
    vm.editor.commands.setContent(doc('hello'), true)
    await flushPromises()
    expect(fake.sendSpy).not.toHaveBeenCalled()
  })

  it('does NOT signal when the update leaves the composer empty', async () => {
    const fake = makeFakeClient()
    setWorkerClient(fake.client)
    const wrapper = await mountComposer({ streamId: 's1' })

    const vm = wrapper.vm as unknown as ComposerVm
    vm.editor.commands.setContent(emptyDoc(), true)
    await flushPromises()
    expect(fake.sendSpy).not.toHaveBeenCalled()
  })

  it('swallows a failed signal (typing must never break composing)', async () => {
    const sendSpy = vi.fn(() => Promise.reject(new Error('offline')))
    setWorkerClient({ typing: { send: sendSpy } } as unknown as WorkerClient)
    const wrapper = await mountComposer({ streamId: 's1' })

    const vm = wrapper.vm as unknown as ComposerVm
    vm.editor.commands.setContent(doc('hello'), true)
    await flushPromises() // no unhandled rejection = pass
    expect(sendSpy).toHaveBeenCalledWith('s1')
    expect(wrapper.find('[data-testid="composer-input"]').exists()).toBe(true)
  })
})
