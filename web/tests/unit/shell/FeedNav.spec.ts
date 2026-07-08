// tests/unit/shell/FeedNav.spec.ts — ENG-136 PR-3. The Feeds entry is expandable:
// its header (nav-feeds) and each sub-item flip the main panel to the Feeds
// placeholder (selectView). Sub-items are collapsed until the toggle is clicked and
// carry small scaffold counts. All SCAFFOLD.
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import FeedNav from '../../../src/components/shell/FeedNav.vue'

describe('FeedNav (ENG-136 PR-3)', () => {
  it('emits selectView from the Feeds header', async () => {
    const wrapper = mount(FeedNav, { props: { active: false } })
    await wrapper.get('[data-testid="nav-feeds"]').trigger('click')
    expect(wrapper.emitted('selectView')).toHaveLength(1)
  })

  it('reveals scaffold sub-items only after expanding', async () => {
    const wrapper = mount(FeedNav, { props: { active: false } })
    // Collapsed: sub-items exist in the tree but hidden via v-show — assert via toggle.
    const toggle = wrapper.get('[data-testid="nav-feeds-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')

    const subs = wrapper.findAll('[data-testid="feed-subitem"]')
    expect(subs.length).toBe(7)
    // The Mentions sub-item carries a scaffold count.
    const mentions = wrapper.get('[data-feed="mentions"]')
    expect(mentions.text()).toContain('Mentions')
    expect(mentions.text()).toContain('3')
  })

  it('emits selectView when a sub-item is clicked', async () => {
    const wrapper = mount(FeedNav, { props: { active: false } })
    await wrapper.get('[data-testid="nav-feeds-toggle"]').trigger('click')
    await wrapper.get('[data-feed="saved"]').trigger('click')
    expect(wrapper.emitted('selectView')).toHaveLength(1)
  })
})
