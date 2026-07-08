import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import IconButton from '../../../src/components/ui/IconButton.vue'

describe('ui/IconButton', () => {
  it('renders with the required aria-label and an icon slot', () => {
    const wrapper = mount(IconButton, {
      props: { ariaLabel: 'Close' },
      slots: { default: '<svg data-testid="icon" />' },
    })
    const btn = wrapper.get('button')
    expect(btn.attributes('aria-label')).toBe('Close')
    expect(btn.find('[data-testid="icon"]').exists()).toBe(true)
    expect(btn.attributes('class') ?? '').toContain('focus-visible:ring-accent')
  })

  it('is square with the md size by default', () => {
    const btn = mount(IconButton, { props: { ariaLabel: 'Menu' } }).get('button')
    expect(btn.classes()).toContain('h-7')
    expect(btn.classes()).toContain('w-7')
  })

  it('throws in dev when aria-label is missing/blank', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // Vue surfaces the setup throw; assert it is our aria-label guard.
    expect(() => mount(IconButton, { props: { ariaLabel: '  ' } })).toThrow(/aria-label/)
    spy.mockRestore()
  })
})
