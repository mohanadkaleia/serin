import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SidebarItem from '../../../src/components/ui/SidebarItem.vue'

describe('ui/SidebarItem', () => {
  it('renders a button with default (secondary) styling and slot label', () => {
    const wrapper = mount(SidebarItem, { slots: { default: '# general' } })
    const root = wrapper.get('button')
    expect(root.text()).toContain('# general')
    expect(root.classes()).toContain('text-secondary')
    expect(root.classes()).toContain('h-7')
  })

  it('shows the active state (accent-subtle bg + left marker)', () => {
    const wrapper = mount(SidebarItem, { props: { active: true }, slots: { default: 'x' } })
    const cls = wrapper.get('button').attributes('class') ?? ''
    expect(cls).toContain('bg-accent-subtle')
    expect(cls).toContain('text-primary')
    // The subtle left accent marker only renders when active.
    expect(wrapper.find('span.bg-accent').exists()).toBe(true)
  })

  it('applies unread styling (primary text + medium weight)', () => {
    const cls =
      mount(SidebarItem, { props: { unread: true }, slots: { default: 'x' } })
        .get('button')
        .attributes('class') ?? ''
    expect(cls).toContain('text-primary')
    expect(cls).toContain('font-medium')
  })

  it('renders as an anchor when href is provided', () => {
    const wrapper = mount(SidebarItem, { props: { href: '/c/general' }, slots: { default: 'x' } })
    const a = wrapper.get('a')
    expect(a.attributes('href')).toBe('/c/general')
  })

  it('renders the trailing slot and passes through data-testid', () => {
    const wrapper = mount(SidebarItem, {
      attrs: { 'data-testid': 'nav-general' },
      slots: { default: 'general', trailing: '<span>3</span>' },
    })
    expect(wrapper.get('button').attributes('data-testid')).toBe('nav-general')
    expect(wrapper.text()).toContain('3')
  })
})
