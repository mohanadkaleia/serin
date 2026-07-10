import { flushPromises, mount } from '@vue/test-utils'
import type { JSONContent } from '@tiptap/core'
import { afterEach, describe, expect, it, vi } from 'vitest'

import MessageComposer from '../../../src/components/shell/MessageComposer.vue'
import type { MentionItem } from '../../../src/components/shell/composer/mentions'

/** A minimal KeyboardEvent stub the editor's `handleKeyDown` accepts. */
function keyEvent(key: string, shiftKey = false): KeyboardEvent {
  return { key, shiftKey, isComposing: false, preventDefault: vi.fn() } as unknown as KeyboardEvent
}

/** Mount the composer and wait for the async TipTap editor to be created. */
async function mountComposer(props: Record<string, unknown> = {}) {
  const wrapper = mount(MessageComposer, { props, attachTo: document.body })
  await flushPromises()
  return wrapper
}

/** The exposed test surface (editor instance + imperative hooks). */
interface ComposerVm {
  editor: {
    commands: {
      setContent: (c: JSONContent, emit?: boolean) => void
      insertContent: (c: string) => void
      clearContent: () => void
    }
    isEmpty: boolean
  }
  submit: () => void
  handleKeyDown: (e: KeyboardEvent) => boolean
}

const vmOf = (wrapper: ReturnType<typeof mount>): ComposerVm => wrapper.vm as unknown as ComposerVm

const doc = (content: JSONContent[]): JSONContent => ({ type: 'doc', content })
const paragraph = (content: JSONContent[]): JSONContent => ({ type: 'paragraph', content })

describe('MessageComposer (TipTap, ENG-101)', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('sends markdown source text + empty mentions on Enter, then clears', async () => {
    const wrapper = await mountComposer()
    const vm = vmOf(wrapper)

    // Type "hi **bold**" as rendered rich text (a bold mark, not literal asterisks).
    vm.editor.commands.setContent(
      doc([
        paragraph([
          { type: 'text', text: 'hi ' },
          { type: 'text', text: 'bold', marks: [{ type: 'bold' }] },
        ]),
      ]),
    )
    await flushPromises()

    expect(vm.handleKeyDown(keyEvent('Enter'))).toBe(true)

    // Serialized BACK to markdown source — the same shape the M2 textarea produced,
    // now with an empty attachment file_ids array (ENG-121).
    expect(wrapper.emitted('send')?.[0]).toEqual(['hi **bold**', [], []])
    // Field cleared after send (M2 parity).
    expect(vm.editor.isEmpty).toBe(true)
  })

  it('resolves @mention chips to user ids in the sent mentions[]', async () => {
    const items: MentionItem[] = [{ id: 'u_dana', label: 'Dana', kind: 'user' }]
    const wrapper = await mountComposer({ mentionItems: items })
    const vm = vmOf(wrapper)

    vm.editor.commands.setContent(
      doc([
        paragraph([
          { type: 'text', text: 'hey ' },
          { type: 'mention', attrs: { id: 'u_dana', label: 'Dana' } },
          { type: 'text', text: ' ping' },
        ]),
      ]),
    )
    await flushPromises()
    vm.submit()

    const [text, mentions, fileIds] = wrapper.emitted('send')?.[0] as [string, string[], string[]]
    expect(text).toBe('hey @Dana ping')
    expect(mentions).toEqual(['u_dana'])
    expect(fileIds).toEqual([])
  })

  it('inserts a newline (does not send) on Shift+Enter', async () => {
    const wrapper = await mountComposer()
    const vm = vmOf(wrapper)
    vm.editor.commands.setContent(doc([paragraph([{ type: 'text', text: 'line one' }])]))
    await flushPromises()

    // Shift+Enter is NOT handled here — it falls through to TipTap's hard break.
    expect(vm.handleKeyDown(keyEvent('Enter', true))).toBe(false)
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('blocks whitespace-only / empty sends and disables the button', async () => {
    const wrapper = await mountComposer()
    const vm = vmOf(wrapper)

    // Empty composer: Enter emits nothing, the send button is disabled.
    vm.handleKeyDown(keyEvent('Enter'))
    expect(wrapper.emitted('send')).toBeUndefined()
    expect(wrapper.get('[data-testid="composer-send"]').attributes('disabled')).toBeDefined()

    // Whitespace-only content is likewise blocked by submit().
    vm.editor.commands.setContent(doc([paragraph([{ type: 'text', text: '   ' }])]))
    await flushPromises()
    vm.submit()
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('emits edit-last on ArrowUp only when empty (ENG-102 seam)', async () => {
    const wrapper = await mountComposer()
    const vm = vmOf(wrapper)

    // Empty → ArrowUp requests editing the last message.
    expect(vm.handleKeyDown(keyEvent('ArrowUp'))).toBe(true)
    expect(wrapper.emitted('edit-last')).toHaveLength(1)

    // Non-empty → ArrowUp is a normal cursor move (not consumed, no emit).
    vm.editor.commands.setContent(doc([paragraph([{ type: 'text', text: 'draft' }])]))
    await flushPromises()
    expect(vm.handleKeyDown(keyEvent('ArrowUp'))).toBe(false)
    expect(wrapper.emitted('edit-last')).toHaveLength(1)
  })

  it('is disabled when no writable stream is selected', async () => {
    const wrapper = await mountComposer({ disabled: true })
    expect(wrapper.get('[data-testid="composer-send"]').attributes('disabled')).toBeDefined()
  })

  it('renders the inner editor with NO border/outline/ring — the card is the only box', async () => {
    const wrapper = await mountComposer()
    const cls = wrapper.get('[data-testid="composer-input"]').attributes('class') ?? ''
    // The ProseMirror node must not draw its own rectangle inside the card…
    expect(cls).not.toMatch(/(^|\s)border(\s|$|-)/)
    expect(cls).not.toMatch(/(^|\s)ring-/)
    // …and must out-specify the global `:focus-visible` accent outline.
    expect(cls).toContain('outline-none')
    expect(cls).toContain('focus:outline-none')
    expect(cls).toContain('focus-visible:outline-none')
  })
})

// -- ENG-152 conversation-pane cleanup: type-@ autocomplete, NO toolbar @ button.
//
// The tiptap Mention suggestion plugin is driven by TYPING `@` in the editor —
// the popup filters the (zero-network) `mentionItems` projection prop and a
// selection inserts the SAME mention node the toolbar path used to, so the
// serialized text + `mentions[]` payload are byte-identical to before.
describe('MessageComposer — type-@ mention autocomplete (ENG-152)', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  const items: MentionItem[] = [
    { id: 'u_dana', label: 'Dana', kind: 'user' },
    { id: 'u_bob', label: 'Bob', kind: 'user' },
    { id: 's_gen', label: 'general', kind: 'channel' },
  ]

  it('renders NO @ toolbar button — mentions are typed, not clicked', async () => {
    const wrapper = await mountComposer({ mentionItems: items })
    expect(wrapper.find('[data-testid="composer-mention-btn"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="Mention someone"]').exists()).toBe(false)
    // The rest of the toolbar (attach/emoji/send) is untouched.
    expect(wrapper.find('[data-testid="attach-file"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-emoji"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-send"]').exists()).toBe(true)
  })

  it('typing @ opens the suggestion popup filtered to matching USERS', async () => {
    const wrapper = await mountComposer({ mentionItems: items })
    const vm = vmOf(wrapper)

    vm.editor.commands.insertContent('@Da')
    await flushPromises()

    // The popup is appended to <body> (mention-popup) with the filtered options:
    // "Da" matches Dana only — Bob and the #general channel are excluded.
    const popup = document.body.querySelector('[data-testid="mention-popup"]')
    expect(popup).not.toBeNull()
    const options = popup!.querySelectorAll('[data-testid="mention-option"]')
    expect(options).toHaveLength(1)
    expect(options[0]!.textContent).toContain('Dana')
    wrapper.unmount()
  })

  it('selecting a suggestion inserts the SAME mention data → mentions[] on send', async () => {
    const wrapper = await mountComposer({ mentionItems: items })
    const vm = vmOf(wrapper)

    vm.editor.commands.insertContent('@Da')
    await flushPromises()

    // jsdom quirk: the Mention extension's commit calls `getSelection().
    // collapseToEnd()`, which THROWS on jsdom's initially-empty selection (a
    // real browser always has one). Seed a trivial selection first.
    const range = document.createRange()
    range.setStart(document.body, 0)
    range.setEnd(document.body, 0)
    window.getSelection()?.addRange(range)

    // Commit the highlighted option (the list commits on mousedown).
    const option = document.body.querySelector('[data-testid="mention-option"]')
    expect(option).not.toBeNull()
    option!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))
    await flushPromises()

    vm.submit()
    const [text, mentions, fileIds] = wrapper.emitted('send')?.[0] as [string, string[], string[]]
    // Identical payload shape to the old toolbar-@ path: markdown source text with
    // the `@label` chip serialization and the resolved `u_` id in mentions[].
    expect(text).toContain('@Dana')
    expect(mentions).toEqual(['u_dana'])
    expect(fileIds).toEqual([])
  })
})
