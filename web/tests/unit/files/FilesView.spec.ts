// tests/unit/files/FilesView.spec.ts — ENG-152: the workspace Files view.
// Rendering from a mocked `client.files.list` (via FakeWorker), uploader-name
// resolution through the directory, source-channel labels, the download action
// (worker blob path — `client.files.download`), the name/type filter, and the
// loading / empty / error states. All data flows through the injected
// WorkerClient — the fake's `fetch` spy proves zero HTTP from this view.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import FilesView from '../../../src/components/files/FilesView.vue'
import { setWorkerClient } from '../../../src/composables/useWorkerClient'
import { useAuthStore } from '../../../src/stores/auth'
import { FakeWorker } from '../shell/fakeWorker'

describe('FilesView (ENG-152)', () => {
  let fake: FakeWorker

  beforeEach(() => {
    setActivePinia(createPinia())
    fake = new FakeWorker()
    fake.setDirectory(
      [
        { user_id: 'u_alice', display_name: 'Alice Anders' },
        { user_id: 'u_bob', display_name: 'Bob Builder' },
      ],
      [],
    )
    fake.addStream({ stream_id: 's_general', name: 'general', kind: 'channel' })
  })

  afterEach(() => {
    setWorkerClient(undefined)
    document.body.innerHTML = ''
  })

  async function mountView(): Promise<VueWrapper> {
    setWorkerClient(fake.client)
    const auth = useAuthStore()
    auth.myUserId = 'u_me'
    const wrapper = mount(FilesView, { attachTo: document.body })
    await flushPromises()
    return wrapper
  }

  it('renders the files from client.files.list with name, size, uploader, channel, date', async () => {
    fake.addFile({
      file_id: 'f_report',
      name: 'report.pdf',
      mime_type: 'application/pdf',
      size_bytes: 4096,
      stream_id: 's_general',
      uploaded_by: 'u_alice',
      created_at: '2026-01-05T10:00:00.000Z',
    })
    const wrapper = await mountView()

    expect(fake.filesListSpy).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="files-view"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="files-list"]').exists()).toBe(true)
    const row = wrapper.get('[data-testid="file-row"]')
    expect(row.get('[data-testid="file-name"]').text()).toBe('report.pdf')
    expect(row.text()).toContain('4.0 KB')
    expect(row.get('[data-testid="file-uploader"]').text()).toBe('Alice Anders')
    expect(row.get('[data-testid="file-channel"]').text()).toBe('# general')
    expect(row.get('[data-testid="file-date"]').text()).not.toBe('')
    // The view read ONLY through the worker client — never HTTP.
    expect(fake.fetch).not.toHaveBeenCalled()
  })

  it('lists newest-first (the worker sort) and keys rows by file id', async () => {
    fake.addFile({ file_id: 'f_old', name: 'old.txt', created_at: '2026-01-01T00:00:00.000Z' })
    fake.addFile({ file_id: 'f_new', name: 'new.txt', created_at: '2026-02-01T00:00:00.000Z' })
    const wrapper = await mountView()

    const names = wrapper
      .findAll('[data-testid="file-name"]')
      .map((n: { text(): string }) => n.text())
    expect(names).toEqual(['new.txt', 'old.txt'])
  })

  it('an uploader missing from the directory falls back to a short id (never a crash)', async () => {
    fake.addFile({ file_id: 'f_x', uploaded_by: 'u_departed_member_123' })
    const wrapper = await mountView()

    expect(wrapper.get('[data-testid="file-uploader"]').text()).toBe('u_depart…')
  })

  it('download triggers client.files.download with the row file id', async () => {
    fake.addFile({ file_id: 'f_dl', name: 'notes.md' })
    const wrapper = await mountView()

    await wrapper.get('[data-testid="file-download"]').trigger('click')
    await flushPromises()

    expect(fake.downloadSpy).toHaveBeenCalledTimes(1)
    expect(fake.downloadSpy).toHaveBeenCalledWith('f_dl')
  })

  it('the filter narrows by name or mime type (distinct empty copy on no match)', async () => {
    fake.addFile({ file_id: 'f_a', name: 'photo.png', mime_type: 'image/png' })
    fake.addFile({ file_id: 'f_b', name: 'doc.pdf', mime_type: 'application/pdf' })
    const wrapper = await mountView()
    expect(wrapper.findAll('[data-testid="file-row"]')).toHaveLength(2)

    await wrapper.get('[data-testid="files-filter"]').setValue('pdf')
    expect(wrapper.findAll('[data-testid="file-row"]')).toHaveLength(1)
    expect(wrapper.get('[data-testid="file-name"]').text()).toBe('doc.pdf')

    await wrapper.get('[data-testid="files-filter"]').setValue('zzz-no-match')
    expect(wrapper.findAll('[data-testid="file-row"]')).toHaveLength(0)
    expect(wrapper.find('[data-testid="files-filter-empty"]').exists()).toBe(true)
    // The TRUE empty state is reserved for a file-less workspace.
    expect(wrapper.find('[data-testid="files-empty"]').exists()).toBe(false)
  })

  it('shows the empty state when the workspace has no files', async () => {
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="files-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="files-list"]').exists()).toBe(false)
  })

  it('a failed load shows the error state; Retry reloads', async () => {
    fake.addFile({ file_id: 'f_later', name: 'later.txt' })
    fake.failNextFilesList('network')
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="files-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="files-list"]').exists()).toBe(false)

    await wrapper.get('[data-testid="files-retry"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="files-error"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="file-row"]')).toHaveLength(1)
    expect(fake.filesListSpy).toHaveBeenCalledTimes(2)
  })
})
