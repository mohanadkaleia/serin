import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ReactionPill from '../../../src/components/shell/ReactionPill.vue'
import type { ReactionAggregate } from '../../../src/worker'

function chip(over: Partial<ReactionAggregate> = {}): ReactionAggregate {
  return {
    emoji: '👍',
    count: 7,
    user_ids: ['u_a', 'u_b'],
    display_names: ['Ann', 'Bo'],
    mine: false,
    ...over,
  }
}

describe('ReactionPill', () => {
  it('renders the emoji, count, and who-reacted tooltip', () => {
    const wrapper = mount(ReactionPill, { props: { chip: chip() } })
    const pill = wrapper.get('[data-testid="reaction-chip"]')
    expect(pill.text()).toContain('👍')
    expect(pill.text()).toContain('7')
    expect(wrapper.get('[data-testid="reaction-tooltip"]').text()).toBe('Ann, Bo')
  })

  it('styles MINE with the accent tint and others neutral', () => {
    const mine = mount(ReactionPill, { props: { chip: chip({ mine: true }) } })
    const mineClasses = mine.get('[data-testid="reaction-chip"]').classes()
    expect(mineClasses).toContain('bg-accent-subtle')
    expect(mineClasses).toContain('text-accent')
    expect(mineClasses).toContain('border-accent')

    const other = mount(ReactionPill, { props: { chip: chip({ mine: false }) } })
    const otherClasses = other.get('[data-testid="reaction-chip"]').classes()
    expect(otherClasses).toContain('bg-surface')
    expect(otherClasses).toContain('border-subtle')
    expect(otherClasses).not.toContain('bg-accent-subtle')
  })

  it('is a rounded-full pill', () => {
    const wrapper = mount(ReactionPill, { props: { chip: chip() } })
    expect(wrapper.get('[data-testid="reaction-chip"]').classes()).toContain('rounded-full')
  })

  it('emits toggle(emoji, mine) on click and disables when not reactable', async () => {
    const wrapper = mount(ReactionPill, { props: { chip: chip({ emoji: '🎉', mine: true }) } })
    await wrapper.get('[data-testid="reaction-chip"]').trigger('click')
    expect(wrapper.emitted('toggle')?.[0]).toEqual(['🎉', true])

    const disabled = mount(ReactionPill, { props: { chip: chip(), disabled: true } })
    expect(
      (disabled.get('[data-testid="reaction-chip"]').element as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('renders opaque emoji / display-name XSS payloads inert (text only)', () => {
    const payload = '<img src=x onerror="window.__pwned=1">'
    const wrapper = mount(ReactionPill, {
      props: { chip: chip({ emoji: payload, display_names: [payload] }) },
    })
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('<img')
    expect(wrapper.get('[data-testid="reaction-chip"]').text()).toContain(payload)
    expect(wrapper.get('[data-testid="reaction-tooltip"]').text()).toBe(payload)
  })
})
