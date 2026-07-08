<script setup lang="ts">
// ThreadSummary — the thread reply affordance under a root (ENG-136 "Ranin" PR-2).
//
// Extracted + restyled from MessageItem's inline `thread-affordance` to match the
// reference: a row of small OVERLAPPING participant avatars, then "{N} replies" in
// the accent color, then an optional muted "Last reply {time}". Clicking opens the
// thread pane. The `thread-affordance` + `thread-reply-count` test-ids and the
// click→open behavior are preserved — the m3-messaging-core E2E asserts
// `thread-affordance` visibility.
//
// SECURITY: participant `display_name`s are other users' content; the initial +
// `title` render ONLY via Vue text/attribute interpolation (escaped, inert).
//
// DATA NOTE: "Last reply {time}" needs the thread's last-reply timestamp, which is
// NOT on `DisplayMessage`/`messages.threads` today — so it is OMITTED until a query
// surfaces it (the reply count + avatars are honest with the data we have).
import type { ThreadParticipant } from '../../worker'

const props = defineProps<{
  replyCount: number
  participants: ThreadParticipant[]
}>()

const emit = defineEmits<{
  /** Open the thread pane on this root. */
  open: []
}>()

/** First letter of a display name for the avatar chip (safe-interpolated). */
function initial(name: string): string {
  const c = name.trim().charAt(0)
  return c ? c.toUpperCase() : '?'
}
</script>

<template>
  <button
    type="button"
    class="mt-1 flex items-center gap-2 rounded-md px-1 py-0.5 text-xs hover:bg-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-background"
    data-testid="thread-affordance"
    @click="emit('open')"
  >
    <span class="flex -space-x-2">
      <span
        v-for="p in props.participants.slice(0, 3)"
        :key="p.user_id"
        class="flex h-5 w-5 items-center justify-center rounded-full border border-background bg-accent-subtle text-[9px] font-semibold text-accent"
        data-testid="thread-participant"
        :title="p.display_name"
        >{{ initial(p.display_name) }}</span
      >
    </span>
    <span class="font-medium text-accent hover:underline" data-testid="thread-reply-count"
      >{{ props.replyCount }} {{ props.replyCount === 1 ? 'reply' : 'replies' }}</span
    >
  </button>
</template>
