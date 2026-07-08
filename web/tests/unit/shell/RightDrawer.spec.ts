// tests/unit/shell/RightDrawer.spec.ts — ENG-136 "Ranin" right drawer (PR-B).
// Asserts the drawer is a "Thread" complementary landmark that mounts <ThreadPane>
// only when open (same open/close behavior as the old conditional aside). ThreadPane
// is stubbed — its own testids are covered by ThreadPane.spec; here we only prove
// the wrapper's mount/unmount + landmark contract.
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RightDrawer from '../../../src/components/shell/RightDrawer.vue'

const stubs = { ThreadPane: { template: '<div data-testid="thread-pane-stub" />' } }

describe('RightDrawer (ENG-136 PR-B)', () => {
  it('renders nothing when closed', () => {
    const wrapper = mount(RightDrawer, { props: { open: false }, global: { stubs } })
    expect(wrapper.find('[role="complementary"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="thread-pane-stub"]').exists()).toBe(false)
  })

  it('mounts ThreadPane as a Thread complementary landmark when open', () => {
    const wrapper = mount(RightDrawer, { props: { open: true }, global: { stubs } })
    const panel = wrapper.get('[role="complementary"]')
    expect(panel.attributes('aria-label')).toBe('Thread')
    expect(wrapper.find('[data-testid="thread-pane-stub"]').exists()).toBe(true)
  })

  it('unmounts the thread pane when toggled closed', async () => {
    const wrapper = mount(RightDrawer, { props: { open: true }, global: { stubs } })
    expect(wrapper.find('[data-testid="thread-pane-stub"]').exists()).toBe(true)
    await wrapper.setProps({ open: false })
    expect(wrapper.find('[data-testid="thread-pane-stub"]').exists()).toBe(false)
  })
})
