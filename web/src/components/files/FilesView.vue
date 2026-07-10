<script setup lang="ts">
// FilesView — ENG-152: the workspace file browser, reached from the sidebar's
// `nav-files` item (the shell's `activeView` flips to 'files' — sections are
// shell panels here, not router routes, matching Inbox/Admin).
//
// DATA: everything flows through the worker client — `client.files.list()` (the
// LOCAL `files` projection, ENG-120: a mirror of the `file.uploaded` events the
// server's sync already scoped to the caller's READABLE streams, so read-authz was
// enforced server-side at delivery time), plus the existing `directory.list` /
// `streams.list` projection queries to resolve uploader names and source-channel
// labels. ZERO network on this path and no token exposure — this view never
// touches HTTP (the `no-http-in-ui` gate covers it).
//
// Rows render name/size/uploader/channel/date (all via escaped text
// interpolation — names are other users' / attacker-controlled input), a
// thumbnail for images, and a Download that reuses the worker blob path
// (FileRowItem, the AttachmentFile discipline). Newest first (worker-sorted);
// a lightweight client-side name/type filter narrows the list.
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'

import EmptyState from '../ui/EmptyState.vue'
import Button from '../ui/Button.vue'
import Icon from '../ui/Icon.vue'
import IconButton from '../ui/IconButton.vue'
import FileRowItem from './FileRowItem.vue'
import { resolveWorkerClient } from '../../composables/useWorkerClient'
import { dmDisplayName, shortUserId } from '../../lib/dm'
import { useAuthStore } from '../../stores/auth'
import type { DmParticipants, FileRow, StreamRow } from '../../worker'

const { myUserId } = storeToRefs(useAuthStore())

const files = ref<FileRow[]>([])
/** `user_id → display_name` from the workspace directory (uploader resolution). */
const names = ref<ReadonlyMap<string, string>>(new Map())
/** `stream_id → stream row` for the source-channel label (incl. DM participants). */
const streams = ref<ReadonlyMap<string, StreamRow & DmParticipants>>(new Map())
const loading = ref(true)
const loadError = ref(false)

/** Client-side name/type narrowing (display-only; never a markup sink). */
const filter = ref('')

async function load(): Promise<void> {
  loading.value = true
  loadError.value = false
  try {
    const client = await resolveWorkerClient()
    // Three LOCAL projection reads (zero network); directory + streams resolve
    // the uploader / source-channel display fields.
    const [listed, directory, streamList] = await Promise.all([
      client.files.list(),
      client.query({ q: 'directory.list' }),
      client.query({ q: 'streams.list' }),
    ])
    files.value = listed.files
    names.value = new Map(directory.users.map((u) => [u.user_id, u.display_name]))
    streams.value = new Map(streamList.streams.map((s) => [s.stream_id, s]))
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
})

/** Case-insensitive substring match over the file name + mime type. */
const visibleFiles = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return files.value
  return files.value.filter(
    (f) => f.name.toLowerCase().includes(q) || f.mime_type.toLowerCase().includes(q),
  )
})

/** The uploader's directory name, or an honest short id for a departed member. */
function uploaderName(file: FileRow): string {
  return names.value.get(file.uploaded_by) ?? shortUserId(file.uploaded_by)
}

/** The source stream's label: `# channel`, the DM counterpart's name, or a short id. */
function channelLabel(file: FileRow): string {
  const stream = streams.value.get(file.stream_id)
  if (!stream) return shortUserId(file.stream_id)
  if (stream.kind === 'dm') {
    return dmDisplayName(stream.dm_user_ids, myUserId.value ?? undefined, names.value) ?? 'DM'
  }
  return `# ${stream.name ?? stream.stream_id}`
}
</script>

<template>
  <section data-testid="files-view" class="flex min-h-0 min-w-0 flex-1 flex-col">
    <!-- Toolbar: the name/type filter + a refresh action. -->
    <div class="flex items-center gap-2 border-b border-subtle px-4 py-2">
      <label class="relative flex-1 max-w-xs">
        <span
          class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted"
          aria-hidden="true"
        >
          <Icon name="search" :size="14" />
        </span>
        <input
          v-model="filter"
          type="search"
          placeholder="Filter by name or type"
          aria-label="Filter files by name or type"
          data-testid="files-filter"
          class="w-full rounded-md border border-subtle bg-surface py-1 pl-7 pr-2 text-[13px] text-primary placeholder:text-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <IconButton label="Refresh files" data-testid="files-refresh" @click="load">
        <Icon name="refresh" :size="14" />
      </IconButton>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto px-4 py-3">
      <div class="mx-auto w-full max-w-4xl">
        <!-- Loading state. -->
        <p v-if="loading" class="px-3 py-6 text-[13px] text-muted" data-testid="files-loading">
          Loading files…
        </p>

        <!-- Load error: calm copy + retry (never a crash). -->
        <div
          v-else-if="loadError"
          class="flex flex-col items-center gap-3 py-10"
          data-testid="files-error"
        >
          <p class="text-[13px] text-secondary">Files couldn’t be loaded.</p>
          <Button variant="ghost" size="sm" data-testid="files-retry" @click="load">Retry</Button>
        </div>

        <!-- Empty workspace (no uploads yet). -->
        <EmptyState
          v-else-if="files.length === 0"
          data-testid="files-empty"
          title="No files yet"
          description="Files shared in your channels and DMs will show up here."
        >
          <template #icon><Icon name="file" :size="20" /></template>
        </EmptyState>

        <!-- A filter that matches nothing (distinct from a truly empty workspace). -->
        <EmptyState
          v-else-if="visibleFiles.length === 0"
          data-testid="files-filter-empty"
          title="No matching files"
          description="Try a different name or type."
        />

        <template v-else>
          <!-- Column header (visual only; each row labels its cells for a11y). -->
          <div
            class="hidden grid-cols-[minmax(0,2.5fr)_minmax(0,1fr)_minmax(0,1fr)_5.5rem_auto] gap-3 border-b border-subtle px-3 pb-2 text-[11px] font-medium uppercase tracking-wide text-muted sm:grid"
            aria-hidden="true"
          >
            <span>Name</span>
            <span>Shared by</span>
            <span>Channel</span>
            <span>Date</span>
            <span class="w-7" />
          </div>

          <ul class="divide-y divide-subtle" data-testid="files-list">
            <FileRowItem
              v-for="file in visibleFiles"
              :key="file.file_id"
              :file="file"
              :uploader-name="uploaderName(file)"
              :channel-label="channelLabel(file)"
            />
          </ul>
        </template>
      </div>
    </div>
  </section>
</template>
