// tests/unit/shell/ProfileDialog.spec.ts — the self-profile dialog (view + edit
// display name). Mounts over the FakeWorker and asserts it reads the profile via
// `me.get`, edits the display name via `me.update` (never a direct HTTP call —
// the no-http-in-ui guard covers the source too), shows read-only email/role +
// presence, and surfaces a coded validation error inline.
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import ProfileDialog from '../../../src/components/profile/ProfileDialog.vue'
import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import { usePresenceStore } from '../../../src/stores/presence'
import { FakeWorker } from './fakeWorker'

async function mountDialog(fake: FakeWorker): Promise<ReturnType<typeof mount>> {
  setWorkerClient(fake.client)
  const wrapper = mount(ProfileDialog, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('ProfileDialog — self-profile view + edit', () => {
  let fake: FakeWorker

  beforeEach(() => {
    setActivePinia(createPinia())
    fake = new FakeWorker()
  })

  afterEach(() => {
    setWorkerClient(undefined)
    document.body.innerHTML = ''
  })

  it('loads the profile via me.get and renders name, email, role', async () => {
    fake.setMeProfile({
      user_id: 'u_me',
      display_name: 'Dana Scully',
      email: 'dana@example.com',
      role: 'admin',
    })
    const wrapper = await mountDialog(fake)

    expect(fake.meGetSpy).toHaveBeenCalledTimes(1)
    const input = wrapper.get('[data-testid="profile-display-name"]')
    expect((input.element as HTMLInputElement).value).toBe('Dana Scully')
    expect(wrapper.get('[data-testid="profile-email"]').text()).toBe('dana@example.com')
    expect(wrapper.get('[data-testid="profile-role"]').text()).toBe('admin')
  })

  it('reflects the presence store status label', async () => {
    const presence = usePresenceStore()
    presence.myUserId = 'u_me'
    presence.statuses = new Map([['u_me', 'offline']])
    fake.setMeProfile({ user_id: 'u_me' })
    const wrapper = await mountDialog(fake)

    expect(wrapper.get('[data-testid="profile-presence"]').text()).toBe('Offline')
  })

  it('disables Save until the name is changed, then saves via me.update', async () => {
    fake.setMeProfile({ display_name: 'Old Name' })
    const wrapper = await mountDialog(fake)

    const save = wrapper.get('[data-testid="profile-save"]')
    // Unchanged → disabled.
    expect((save.element as HTMLButtonElement).disabled).toBe(true)

    const input = wrapper.get('[data-testid="profile-display-name"]')
    await input.setValue('New Name')
    expect((save.element as HTMLButtonElement).disabled).toBe(false)

    await save.trigger('click')
    await flushPromises()

    expect(fake.meUpdateSpy).toHaveBeenCalledWith({ display_name: 'New Name' })
    expect(wrapper.find('[data-testid="profile-saved"]').exists()).toBe(true)
    // After save it is unchanged again → disabled.
    expect((save.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not save an empty or whitespace-only name', async () => {
    fake.setMeProfile({ display_name: 'Keep' })
    const wrapper = await mountDialog(fake)

    const input = wrapper.get('[data-testid="profile-display-name"]')
    await input.setValue('   ')
    const save = wrapper.get('[data-testid="profile-save"]')
    expect((save.element as HTMLButtonElement).disabled).toBe(true)
    await save.trigger('click')
    await flushPromises()
    expect(fake.meUpdateSpy).not.toHaveBeenCalled()
  })

  it('surfaces a coded validation error inline and keeps the dialog open', async () => {
    fake.setMeProfile({ display_name: 'Old' })
    fake.failNextMeUpdate('validation-error')
    const wrapper = await mountDialog(fake)

    await wrapper.get('[data-testid="profile-display-name"]').setValue('Changed')
    await wrapper.get('[data-testid="profile-save"]').trigger('click')
    await flushPromises()

    const err = wrapper.get('[data-testid="profile-error"]')
    expect(err.text()).toContain('1 and 200')
    expect(wrapper.find('[data-testid="profile-saved"]').exists()).toBe(false)
  })

  it('emits close on the Close button', async () => {
    const wrapper = await mountDialog(fake)
    await wrapper.get('[data-testid="profile-close"]').trigger('click')
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('shows a retryable error state when the load fails', async () => {
    fake.failNextMeGet('network')
    const wrapper = await mountDialog(fake)

    expect(wrapper.find('[data-testid="profile-load-error"]').exists()).toBe(true)
    await wrapper.get('[data-testid="profile-retry"]').trigger('click')
    await flushPromises()
    // The retry loaded the profile — the editable field is now present.
    expect(wrapper.find('[data-testid="profile-display-name"]').exists()).toBe(true)
  })
})
