// tests/unit/ui/PresenceDot.spec.ts — ENG-128. The presence dot is a dumb,
// token-styled circle: `bg-success` when online, `bg-muted` when offline, with an
// additive `presence-dot` test-id + `data-status` so E2E can read the live state.
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PresenceDot from '../../../src/components/ui/PresenceDot.vue'

describe('ui/PresenceDot (ENG-128)', () => {
  it('renders a success-toned dot when online', () => {
    const wrapper = mount(PresenceDot, { props: { status: 'online' } })
    const dot = wrapper.get('[data-testid="presence-dot"]')
    expect(dot.classes()).toContain('bg-success')
    expect(dot.classes()).not.toContain('bg-muted')
    expect(dot.attributes('data-status')).toBe('online')
  })

  it('renders a muted dot when offline', () => {
    const wrapper = mount(PresenceDot, { props: { status: 'offline' } })
    const dot = wrapper.get('[data-testid="presence-dot"]')
    expect(dot.classes()).toContain('bg-muted')
    expect(dot.classes()).not.toContain('bg-success')
    expect(dot.attributes('data-status')).toBe('offline')
  })

  it('sizes md by default and sm on request', () => {
    const md = mount(PresenceDot, { props: { status: 'online' } })
    expect(md.get('[data-testid="presence-dot"]').classes()).toContain('h-2.5')

    const sm = mount(PresenceDot, { props: { status: 'online', size: 'sm' } })
    expect(sm.get('[data-testid="presence-dot"]').classes()).toContain('h-2')
  })

  it('is decorative (aria-hidden) and merges caller positioning classes', () => {
    const wrapper = mount(PresenceDot, {
      props: { status: 'online' },
      attrs: { class: 'absolute -bottom-0.5 -right-0.5' },
    })
    const dot = wrapper.get('[data-testid="presence-dot"]')
    expect(dot.attributes('aria-hidden')).toBe('true')
    expect(dot.classes()).toContain('absolute')
    expect(dot.classes()).toContain('bg-success')
  })
})
