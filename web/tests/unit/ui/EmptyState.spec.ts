import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EmptyState from '../../../src/components/ui/EmptyState.vue'

describe('ui/EmptyState', () => {
  it('renders title and description', () => {
    const wrapper = mount(EmptyState, {
      props: { title: 'No channels yet', description: 'Create one to get started.' },
    })
    expect(wrapper.text()).toContain('No channels yet')
    expect(wrapper.text()).toContain('Create one to get started.')
  })

  it('omits the description paragraph when not provided', () => {
    const wrapper = mount(EmptyState, { props: { title: 'Empty' } })
    expect(wrapper.text()).toContain('Empty')
    expect(wrapper.findAll('p')).toHaveLength(1)
  })

  it('renders icon and action slots', () => {
    const wrapper = mount(EmptyState, {
      props: { title: 'Empty' },
      slots: {
        icon: '<svg data-testid="icon" />',
        action: '<button data-testid="cta">Add</button>',
      },
    })
    expect(wrapper.find('[data-testid="icon"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cta"]').exists()).toBe(true)
  })
})
