// tests/unit/shell/SpaceRail.spec.ts — ENG-136 "Ranin" left rail (PR-B). Asserts
// the rail landmark, the neutral workspace glyph, the relocated GLOBAL sync
// indicator (single `sync-indicator` testid, tone-driven from the sync store), and
// the account sign-out affordance.
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import SpaceRail from '../../../src/components/shell/SpaceRail.vue'
import { useSyncStore } from '../../../src/stores/sync'

function mountRail(): ReturnType<typeof mount> {
  return mount(SpaceRail, {
    props: { workspaceInitials: 'MS', workspaceName: 'msg' },
  })
}

describe('SpaceRail (ENG-136 PR-B)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('is a Workspaces navigation landmark with the neutral workspace glyph', () => {
    const wrapper = mountRail()
    const nav = wrapper.get('nav')
    expect(nav.attributes('role')).toBe('navigation')
    expect(nav.attributes('aria-label')).toBe('Workspaces')
    // Neutral initials — NOT "Ranin".
    expect(wrapper.text()).toContain('MS')
    expect(wrapper.text()).not.toContain('Ranin')
  })

  it('hosts the single global sync indicator, tone-driven from the sync store', async () => {
    const wrapper = mountRail()
    const sync = useSyncStore()

    // Exactly one sync-indicator lives in the rail (uniqueness for the e2e selector).
    expect(wrapper.findAll('[data-testid="sync-indicator"]')).toHaveLength(1)

    sync.status = { state: 'live', online: true }
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="sync-indicator"]').attributes('data-tone')).toBe('live')

    sync.status = { state: 'degraded', online: false }
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="sync-indicator"]').attributes('data-tone')).toBe('offline')
  })

  it('emits logout from the account affordance', async () => {
    const wrapper = mountRail()
    await wrapper.get('[data-testid="logout"]').trigger('click')
    expect(wrapper.emitted('logout')).toHaveLength(1)
  })
})
