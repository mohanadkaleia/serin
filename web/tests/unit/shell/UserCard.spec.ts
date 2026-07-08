// tests/unit/shell/UserCard.spec.ts — ENG-136 PR-3. The footer user card renders the
// REAL display name, an initial avatar, and a presence dot whose color reflects the
// REAL status (bg-success online, bg-muted offline) with an honest label.
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import UserCard from '../../../src/components/shell/UserCard.vue'

describe('UserCard (ENG-136 PR-3)', () => {
  it('shows the name, initial and an online dot + label by default', () => {
    const wrapper = mount(UserCard, { props: { name: 'Dana Scully' } })
    expect(wrapper.text()).toContain('Dana Scully')
    expect(wrapper.text()).toContain('Online')
    // Avatar initial.
    expect(wrapper.text()).toContain('D')
    // The presence dot is success-toned when online.
    const dot = wrapper.get('[data-status="online"]')
    expect(dot.classes()).toContain('bg-success')
  })

  it('reflects an offline status with a muted dot + label', () => {
    const wrapper = mount(UserCard, { props: { name: 'Sam', status: 'offline' } })
    expect(wrapper.text()).toContain('Offline')
    const dot = wrapper.get('[data-status="offline"]')
    expect(dot.classes()).toContain('bg-muted')
  })

  it('exposes the user-card test-id', () => {
    const wrapper = mount(UserCard, { props: { name: 'Sam' } })
    expect(wrapper.find('[data-testid="user-card"]').exists()).toBe(true)
  })
})
