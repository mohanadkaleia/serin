// lib/commands — the Cmd+K command registry (ENG-136). Pure over injected
// seams: each command's `run` fires EXACTLY its own seam, and `available()`
// context-gates the channel-notifications command. No dead actions: every
// registered command maps to a live shell seam.
import { describe, expect, it, vi } from 'vitest'

import { buildCommands, type CommandSeams } from '../../../src/lib/commands'

function makeSeams(overrides: Partial<CommandSeams> = {}): CommandSeams {
  return {
    openCreateChannel: vi.fn(),
    openNewDm: vi.fn(),
    openChannelBrowser: vi.fn(),
    openSearch: vi.fn(),
    cycleTheme: vi.fn(),
    goToInbox: vi.fn(),
    openChannelNotifications: vi.fn(),
    hasActiveChannel: () => true,
    signOut: vi.fn(),
    ...overrides,
  }
}

/** Which seam each command id must (exclusively) fire. */
const WIRING: Array<[id: string, seam: keyof CommandSeams]> = [
  ['create-channel', 'openCreateChannel'],
  ['start-dm', 'openNewDm'],
  ['browse-channels', 'openChannelBrowser'],
  ['search-messages', 'openSearch'],
  ['go-inbox', 'goToInbox'],
  ['channel-notifications', 'openChannelNotifications'],
  ['toggle-theme', 'cycleTheme'],
  ['sign-out', 'signOut'],
]

describe('buildCommands (ENG-136 palette actions)', () => {
  it('registers exactly the seamed commands, in a stable order', () => {
    const commands = buildCommands(makeSeams())
    expect(commands.map((c) => c.id)).toEqual(WIRING.map(([id]) => id))
    // Display contract: every command carries a title + a leading icon.
    for (const c of commands) {
      expect(c.title.length).toBeGreaterThan(0)
      expect(c.icon.length).toBeGreaterThan(0)
    }
  })

  it.each(WIRING)('%s runs ONLY its %s seam', (id, seamName) => {
    const seams = makeSeams()
    const command = buildCommands(seams).find((c) => c.id === id)!
    command.run()

    for (const [, other] of WIRING) {
      const spy = seams[other] as ReturnType<typeof vi.fn>
      expect(spy).toHaveBeenCalledTimes(other === seamName ? 1 : 0)
    }
  })

  it('channel-notifications is the ONLY context-gated command, via hasActiveChannel', () => {
    const withChannel = buildCommands(makeSeams({ hasActiveChannel: () => true }))
    const withoutChannel = buildCommands(makeSeams({ hasActiveChannel: () => false }))

    for (const c of withChannel) {
      if (c.id === 'channel-notifications') expect(c.available?.()).toBe(true)
      else expect(c.available).toBeUndefined()
    }
    const gated = withoutChannel.find((c) => c.id === 'channel-notifications')!
    expect(gated.available?.()).toBe(false)
  })
})
