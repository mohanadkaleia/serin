<script setup lang="ts">
// FileRowItem — one row of the workspace Files view (ENG-152): a thumbnail (for
// images) or a file glyph, the name + human size, the resolved uploader, the
// source channel, the upload date, and a Download action.
//
// SECURITY (mirrors AttachmentFile/MessageItem): `file.name` and `file.mime_type`
// are ATTACKER-CONTROLLED, and `uploaderName` is another user's input. Everything
// renders ONLY through Vue text interpolation ({{ }}), which HTML-escapes; there is
// NO v-html / innerHTML / dynamic `:is` anywhere here. The `download` attribute
// receives the attacker name, but it is an inert suggested filename the browser
// sanitizes — never a script or markup sink.
//
// TOKEN BOUNDARY: bytes come through `client.files.download` (all fetch/token/HTTP
// stays worker-side) into a ONE-SHOT local `blob:` URL for the `<a download>`
// click, revoked immediately after — exactly the AttachmentFile discipline. The
// image preview reuses `useFileUrl` (the refcounted thumbnail path, ENG-119/121),
// which 404-degrades to the plain glyph.
import { computed } from 'vue'

import Icon from '../ui/Icon.vue'
import IconButton from '../ui/IconButton.vue'
import { resolveWorkerClient } from '../../composables/useWorkerClient'
import { useFileUrl } from '../../composables/useFileUrl'
import { formatBytes } from '../../lib/bytes'
import { formatDayDivider } from '../../lib/time'
import type { FileRow } from '../../worker'

const props = defineProps<{
  file: FileRow
  /** The uploader's directory display name (falls back to a short id upstream). */
  uploaderName: string
  /** The source stream's label (`# channel` / DM participants / a short id). */
  channelLabel: string
}>()

/** Only image uploads get a thumbnail attempt (the server 404s non-images). */
const isImage = computed(() => props.file.mime_type.startsWith('image/'))

// The shared refcounted thumbnail URL (null until loaded / permanently on a 404,
// which falls back to the glyph). Fetched only for image rows.
const { url: thumbUrl } = isImage.value
  ? useFileUrl(props.file.file_id, 'thumbnail')
  : { url: computed(() => null) }

/** Upload date from the projected `created_at` (client-claimed ISO); "—" if unparseable. */
const dateLabel = computed(() => {
  const ms = Date.parse(props.file.created_at)
  return Number.isNaN(ms) ? '—' : formatDayDivider(ms)
})

/** Download the blob and trigger a browser save via a transient `<a download>`. */
async function onDownload(): Promise<void> {
  const client = await resolveWorkerClient()
  const { blob } = await client.files.download(props.file.file_id)
  if (!blob) return
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  // Attacker-controlled name → an INERT suggested filename (browser-sanitized).
  a.download = props.file.name
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoke on the next tick so the click has consumed the URL.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
</script>

<template>
  <li
    class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 py-2 transition-colors hover:bg-surface sm:grid-cols-[minmax(0,2.5fr)_minmax(0,1fr)_minmax(0,1fr)_5.5rem_auto]"
    data-testid="file-row"
    :data-file-id="file.file_id"
  >
    <!-- Name cell: thumbnail/glyph + name over size. -->
    <div class="flex min-w-0 items-center gap-3">
      <span
        class="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-md border border-subtle bg-surface text-muted"
        aria-hidden="true"
      >
        <img
          v-if="thumbUrl"
          :src="thumbUrl"
          alt=""
          class="h-full w-full object-cover"
          data-testid="file-thumbnail"
        />
        <Icon v-else name="file" :size="16" />
      </span>
      <div class="min-w-0">
        <!-- ATTACKER-CONTROLLED name — text interpolation only (escaped). -->
        <p class="truncate text-[13px] font-medium text-primary" data-testid="file-name">
          {{ file.name }}
        </p>
        <p class="truncate text-[12px] text-muted">{{ formatBytes(file.size_bytes) }}</p>
      </div>
    </div>

    <!-- Uploader (directory name; other users' input — escaped interpolation). -->
    <p class="hidden truncate text-[12px] text-secondary sm:block" data-testid="file-uploader">
      {{ uploaderName }}
    </p>

    <!-- Source channel / DM. -->
    <p class="hidden truncate text-[12px] text-secondary sm:block" data-testid="file-channel">
      {{ channelLabel }}
    </p>

    <!-- Upload date. -->
    <p class="hidden truncate text-[12px] text-muted sm:block" data-testid="file-date">
      {{ dateLabel }}
    </p>

    <IconButton label="Download" data-testid="file-download" @click="onDownload">
      <Icon name="download" :size="16" />
    </IconButton>
  </li>
</template>
