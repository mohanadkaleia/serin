import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import Icon from '../../../src/components/ui/Icon.vue'

describe('ui/Icon', () => {
  it('renders the mapped lucide component as an svg for a known name', () => {
    const wrapper = mount(Icon, { props: { name: 'send' } })
    const svg = wrapper.get('svg')
    // lucide tags every glyph with its icon class — proves the `send` row mapped.
    expect(svg.classes().join(' ')).toContain('lucide-send')
  })

  it('applies the default size (18) and stroke attributes', () => {
    const svg = mount(Icon, { props: { name: 'plus' } }).get('svg')
    expect(svg.attributes('width')).toBe('18')
    expect(svg.attributes('height')).toBe('18')
    expect(svg.attributes('stroke')).toBe('currentColor')
    expect(svg.attributes('stroke-width')).toBe('1.75')
  })

  it('honors an explicit size', () => {
    const svg = mount(Icon, { props: { name: 'smile', size: 24 } }).get('svg')
    expect(svg.attributes('width')).toBe('24')
    expect(svg.attributes('height')).toBe('24')
  })

  it('is decorative (aria-hidden, no role) when no label is passed', () => {
    const svg = mount(Icon, { props: { name: 'at-sign' } }).get('svg')
    expect(svg.attributes('aria-hidden')).toBe('true')
    expect(svg.attributes('role')).toBeUndefined()
    expect(svg.attributes('aria-label')).toBeUndefined()
  })

  it('maps the ENG-136 nav glyphs added in PR-3', () => {
    const cases = [
      ['settings', 'lucide-settings'],
      ['mail', 'lucide-mail'],
      ['users', 'lucide-users'],
      ['grid', 'lucide-layout-grid'],
      ['file', 'lucide-file'],
      ['shield', 'lucide-shield'],
      ['square-pen', 'lucide-square-pen'],
      ['bell', 'lucide-bell'],
      ['chevrons-left-right', 'lucide-chevrons-left-right'],
    ] as const
    for (const [name, cls] of cases) {
      const svg = mount(Icon, { props: { name } }).get('svg')
      expect(svg.classes().join(' ')).toContain(cls)
    }
  })

  it('is a semantic image (role=img + aria-label, not hidden) when labeled', () => {
    const svg = mount(Icon, { props: { name: 'paperclip', label: 'Attachment' } }).get('svg')
    expect(svg.attributes('role')).toBe('img')
    expect(svg.attributes('aria-label')).toBe('Attachment')
    expect(svg.attributes('aria-hidden')).toBeUndefined()
  })
})
