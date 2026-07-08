import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ThreadSummary from '../../../src/components/shell/ThreadSummary.vue'
import type { ThreadParticipant } from '../../../src/worker'

const participants: ThreadParticipant[] = [
  { user_id: 'u_a', display_name: 'Ann' },
  { user_id: 'u_b', display_name: 'Bo' },
]

describe('ThreadSummary', () => {
  it('renders overlapping participant avatars and the reply count', () => {
    const wrapper = mount(ThreadSummary, { props: { replyCount: 3, participants } })
    expect(wrapper.get('[data-testid="thread-reply-count"]').text()).toBe('3 replies')
    const avatars = wrapper.findAll('[data-testid="thread-participant"]')
    expect(avatars).toHaveLength(2)
    expect(avatars[0]!.text()).toBe('A')
    // Overlapping avatar stack.
    expect(wrapper.get('[data-testid="thread-affordance"]').find('.-space-x-2').exists()).toBe(true)
  })

  it('caps the avatar stack at three participants', () => {
    const many: ThreadParticipant[] = [
      { user_id: 'u_a', display_name: 'Ann' },
      { user_id: 'u_b', display_name: 'Bo' },
      { user_id: 'u_c', display_name: 'Cy' },
      { user_id: 'u_d', display_name: 'Di' },
    ]
    const wrapper = mount(ThreadSummary, { props: { replyCount: 9, participants: many } })
    expect(wrapper.findAll('[data-testid="thread-participant"]')).toHaveLength(3)
  })

  it('singularizes a single reply', () => {
    const wrapper = mount(ThreadSummary, { props: { replyCount: 1, participants } })
    expect(wrapper.get('[data-testid="thread-reply-count"]').text()).toBe('1 reply')
  })

  it('emits open on click', async () => {
    const wrapper = mount(ThreadSummary, { props: { replyCount: 2, participants } })
    await wrapper.get('[data-testid="thread-affordance"]').trigger('click')
    expect(wrapper.emitted('open')).toHaveLength(1)
  })

  it('renders a participant display-name XSS payload as inert text', () => {
    const payload = '<img src=x onerror="window.__pwned=1">Eve'
    const wrapper = mount(ThreadSummary, {
      props: { replyCount: 1, participants: [{ user_id: 'u_e', display_name: payload }] },
    })
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.get('[data-testid="thread-participant"]').attributes('title')).toBe(payload)
  })
})
