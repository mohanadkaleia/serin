<script setup lang="ts">
// PresenceDot — a tiny live-presence status dot (ENG-128). DUMB + token-styled:
// `bg-success` when online, `bg-muted` when offline (the same palette as the
// UserCard's footer dot). Positioning (corner-anchoring on an avatar, a border
// matching the surface behind it) is the CALLER's concern via the class
// attribute — this component is only the colored circle itself.
//
// Presence is EPHEMERAL (worker-owned, memory-only); callers derive `status`
// from `usePresenceStore().statusOf(userId)` — never from HTTP.
import { computed } from 'vue'

import type { PresenceStatus } from '../../worker'

const props = withDefaults(
  defineProps<{
    /** The user's live status (`offline` when unknown — the store's default). */
    status: PresenceStatus
    /** `md` (10px) suits 28–40px avatars; `sm` (8px) suits inline/dense rows. */
    size?: 'sm' | 'md'
  }>(),
  { size: 'md' },
)

const online = computed(() => props.status === 'online')
</script>

<template>
  <span
    aria-hidden="true"
    class="inline-block rounded-full"
    :class="[size === 'sm' ? 'h-2 w-2' : 'h-2.5 w-2.5', online ? 'bg-success' : 'bg-muted']"
    data-testid="presence-dot"
    :data-status="status"
  />
</template>
