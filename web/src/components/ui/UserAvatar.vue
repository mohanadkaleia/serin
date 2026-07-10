<script setup lang="ts">
// UserAvatar — the shared avatar atom (ENG-152): a member's profile IMAGE when
// their directory record carries an `avatar_sha256`, else the existing initials
// chip. Used everywhere an initial rendered before (message rows, the sidebar
// footer UserCard, DM rows, mention rows, the profile dialog), so one component
// owns the image-vs-initials decision and the load-failure fallback.
//
// DUMB by design (repo convention): the caller passes `userId` + `sha` (both off
// the folded DirectoryUser) and `name`; sizing/shape/colors come from the
// caller's classes on the root (attrs fall through). The image bytes arrive via
// useAvatarUrl → the worker's `user.avatar` RPC — no HTTP call, token, or API
// path here (no-http-in-ui). With no `sha` the component is fully synchronous
// (initials only, zero worker traffic).
//
// SECURITY: `name` is user-controlled — rendered via text interpolation only.
// The <img> src is a tab-minted `blob:` object URL over server-re-encoded bytes.
import { computed, ref, watch } from 'vue'

import { useAvatarUrl } from '../../composables/useAvatarUrl'

const props = defineProps<{
  /** The member's user id (undefined = unknown → initials only). */
  userId?: string | undefined
  /** Display name the initial falls back to (raw id upstream when unnamed). */
  name: string
  /** The directory-carried `avatar_sha256` (undefined = no avatar → initials). */
  sha?: string | undefined
}>()

const initial = computed(() => {
  const c = props.name.trim().charAt(0)
  return c ? c.toUpperCase() : '?'
})

/** A failed <img> load falls back to initials until the sha changes. */
const failed = ref(false)
watch(
  () => props.sha,
  () => {
    failed.value = false
  },
)

const { url } = useAvatarUrl(() => ({ userId: props.userId, sha: props.sha }))
const showImage = computed(() => url.value !== null && !failed.value)
</script>

<template>
  <span class="overflow-hidden" :data-has-image="showImage ? 'true' : undefined">
    <img
      v-if="showImage"
      :src="url ?? undefined"
      alt=""
      class="h-full w-full rounded-[inherit] object-cover"
      @error="failed = true"
    />
    <template v-else>{{ initial }}</template>
  </span>
</template>
