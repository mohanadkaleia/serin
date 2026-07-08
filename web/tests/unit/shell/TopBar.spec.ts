// tests/unit/shell/TopBar.spec.ts — ENG-136 PR-3. The top bar hosts a centered
// search (opens the palette via a `search` event), a REAL compose action (new DM),
// and SCAFFOLD bell/more actions. The search carries the `topbar-search` test-id.
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import TopBar from '../../../src/components/shell/TopBar.vue'

describe('TopBar (ENG-136 PR-3)', () => {
  it('emits search from the centered search button (topbar-search)', async () => {
    const wrapper = mount(TopBar)
    const search = wrapper.get('[data-testid="topbar-search"]')
    expect(search.text()).toContain('Search anything…')
    expect(search.text()).toContain('⌘K')
    await search.trigger('click')
    expect(wrapper.emitted('search')).toHaveLength(1)
  })

  it('emits compose from the compose action', async () => {
    const wrapper = mount(TopBar)
    const compose = wrapper.get('button[aria-label="New message"]')
    await compose.trigger('click')
    expect(wrapper.emitted('compose')).toHaveLength(1)
  })

  it('shows a scaffold notifications bell with an unread dot', () => {
    const wrapper = mount(TopBar)
    expect(wrapper.find('button[aria-label="Notifications"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="topbar-bell-dot"]').exists()).toBe(true)
    expect(wrapper.find('button[aria-label="More"]').exists()).toBe(true)
  })
})
