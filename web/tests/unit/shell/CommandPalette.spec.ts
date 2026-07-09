// CommandPalette (ENG-136 upgrade): one Cmd+K surface, two GROUPS — registered
// Commands (actions) above the original fuzzy channel/DM navigation. The query
// filters BOTH; ↑/↓ traverse the whole list; Enter runs a command (`run`) or
// jumps to a stream (`select`); Esc closes. The palette is presentational —
// command execution stays with the controller (see useShellController.spec.ts).
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CommandPalette, {
  type CommandItem,
  type QuickItem,
} from '../../../src/components/shell/CommandPalette.vue'

const ITEMS: QuickItem[] = [
  { id: 's_general', label: 'general', kind: 'channel', unread: 0 },
  { id: 's_random', label: 'random', kind: 'channel', unread: 2 },
  { id: 's_design', label: 'design', kind: 'channel', unread: 0 },
  { id: 's_dm', label: 'Zoe Zapp', kind: 'dm', unread: 0 },
]

const COMMANDS: CommandItem[] = [
  { id: 'create-channel', title: 'Create channel', icon: 'plus', keywords: 'new add' },
  { id: 'start-dm', title: 'Start a direct message', icon: 'message-square', keywords: 'dm' },
  { id: 'toggle-theme', title: 'Toggle theme', icon: 'moon', keywords: 'dark light mode' },
]

function open(overrides: Partial<{ items: QuickItem[]; commands: CommandItem[] }> = {}) {
  return mount(CommandPalette, {
    props: { open: true, items: ITEMS, commands: COMMANDS, ...overrides },
  })
}

describe('CommandPalette', () => {
  it('is hidden until opened', async () => {
    const wrapper = mount(CommandPalette, {
      props: { open: false, items: ITEMS, commands: COMMANDS },
    })
    expect(wrapper.find('[data-testid="command-palette"]').exists()).toBe(false)

    await wrapper.setProps({ open: true })
    expect(wrapper.find('[data-testid="command-palette"]').exists()).toBe(true)
  })

  it('shows BOTH groups (Commands above Channels & DMs) on an empty query', () => {
    const wrapper = open()

    const text = wrapper.get('[data-testid="command-palette"]').text()
    expect(text).toContain('Commands')
    expect(text).toContain('Channels & DMs')
    // Commands render with per-id testids; streams keep the original testid.
    expect(wrapper.find('[data-testid="palette-command-create-channel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="palette-command-toggle-theme"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="command-palette-item"]')).toHaveLength(ITEMS.length)
    // Group order: the Commands header precedes the navigation header.
    expect(text.indexOf('Commands')).toBeLessThan(text.indexOf('Channels & DMs'))
  })

  it('fuzzy-filters BOTH groups by the query', async () => {
    const wrapper = open()
    const input = wrapper.get('[data-testid="command-palette-input"]')

    // A stream-only query: commands drop out, one channel remains.
    await input.setValue('rand')
    expect(wrapper.findAll('[data-testid="command-palette-item"]')).toHaveLength(1)
    expect(wrapper.get('[data-testid="command-palette-item"]').text()).toContain('random')
    expect(wrapper.findAll('[data-testid^="palette-command-"]')).toHaveLength(0)

    // A command-only query: streams drop out, the matching command remains.
    await input.setValue('create ch')
    expect(wrapper.findAll('[data-testid="command-palette-item"]')).toHaveLength(0)
    expect(wrapper.find('[data-testid="palette-command-create-channel"]').exists()).toBe(true)
  })

  it('matches a command by its keywords (e.g. "dark" → Toggle theme)', async () => {
    const wrapper = open()
    await wrapper.get('[data-testid="command-palette-input"]').setValue('dark')
    expect(wrapper.find('[data-testid="palette-command-toggle-theme"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="palette-command-create-channel"]').exists()).toBe(false)
  })

  it('Enter on the highlighted command emits run(id), not select', async () => {
    const wrapper = open()
    const input = wrapper.get('[data-testid="command-palette-input"]')

    // Index 0 is the first command on an empty query.
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('run')?.[0]).toEqual(['create-channel'])
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('arrows traverse ACROSS the groups: commands first, then streams', async () => {
    const wrapper = open()
    const input = wrapper.get('[data-testid="command-palette-input"]')

    // Step past the 3 commands onto the first stream row.
    for (let i = 0; i < COMMANDS.length; i++) {
      await input.trigger('keydown', { key: 'ArrowDown' })
    }
    expect(wrapper.get('[data-testid="command-palette-item"]').attributes('data-active')).toBe(
      'true',
    )
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('select')?.[0]).toEqual(['s_general'])
    expect(wrapper.emitted('run')).toBeUndefined()

    // ArrowUp from the top wraps to the LAST row (the final stream).
    const fresh = open()
    const freshInput = fresh.get('[data-testid="command-palette-input"]')
    await freshInput.trigger('keydown', { key: 'ArrowUp' })
    await freshInput.trigger('keydown', { key: 'Enter' })
    expect(fresh.emitted('select')?.[0]).toEqual(['s_dm'])
  })

  it('fuzzy-filters streams as the user types and Enter navigates to the match', async () => {
    const wrapper = open()
    const input = wrapper.get('[data-testid="command-palette-input"]')

    await input.setValue('gen')
    const results = wrapper.findAll('[data-testid="command-palette-item"]')
    expect(results).toHaveLength(1)
    expect(results[0]!.text()).toContain('general')

    // The highlight clamps into the shrunken list, so Enter picks the channel
    // (any command matches for 'gen' rank above it — walk to the stream row).
    const commandCount = wrapper.findAll('[data-testid^="palette-command-"]').length
    for (let i = 0; i < commandCount; i++) {
      await input.trigger('keydown', { key: 'ArrowDown' })
    }
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('select')?.[0]).toEqual(['s_general'])
  })

  it('clicking a command row runs it; clicking a stream row selects it', async () => {
    const wrapper = open()

    await wrapper.get('[data-testid="palette-command-toggle-theme"]').trigger('click')
    expect(wrapper.emitted('run')?.[0]).toEqual(['toggle-theme'])

    await wrapper.get('[data-testid="command-palette-item"]').trigger('click')
    expect(wrapper.emitted('select')?.[0]).toEqual(['s_general'])
  })

  it('renders a # icon for channels and an initial avatar for DMs', () => {
    const wrapper = open()
    const rows = wrapper.findAll('[data-testid="command-palette-item"]')
    // Channel row leads with the hash icon (an inline SVG), no initial bubble.
    expect(rows[0]!.find('svg').exists()).toBe(true)
    // DM row leads with the participant's initial.
    const dmRow = rows[rows.length - 1]!
    expect(dmRow.text()).toContain('Zoe Zapp')
    expect(dmRow.find('span.rounded-full').text()).toBe('Z')
  })

  it('shows "No matches" when neither group matches', async () => {
    const wrapper = open()
    await wrapper.get('[data-testid="command-palette-input"]').setValue('zzzzqqq')
    expect(wrapper.get('[data-testid="command-palette"]').text()).toContain('No matches')
  })

  it('closes on Escape', async () => {
    const wrapper = open()
    await wrapper.get('[data-testid="command-palette-input"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toBeTruthy()
  })
})
