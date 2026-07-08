import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import ThemeToggle from '../../../src/components/ui/ThemeToggle.vue'

// jsdom has no matchMedia; useTheme guards that, resolving 'system' -> light.
// This env's window.localStorage is a bare object, so install a working stub.
function installLocalStorage(): void {
  const store = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    writable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    },
  })
}

describe('ui/ThemeToggle', () => {
  beforeEach(() => {
    installLocalStorage()
  })

  it('is a keyboard-operable button with an aria-label reflecting the preference', () => {
    const wrapper = mount(ThemeToggle)
    const btn = wrapper.get('button')
    expect(btn.attributes('aria-label')).toBeTruthy()
    expect(btn.attributes('class') ?? '').toContain('focus-visible:ring-accent')
  })

  it('cycles the preference on click and persists it', async () => {
    const wrapper = mount(ThemeToggle)
    const btn = wrapper.get('button')
    const before = btn.attributes('aria-label') ?? ''
    await btn.trigger('click')
    const after = btn.attributes('aria-label') ?? ''
    expect(after).not.toBe(before)
    // A concrete preference is now persisted (one of the three).
    expect(['light', 'dark', 'system']).toContain(window.localStorage.getItem('msg:theme'))
  })
})
