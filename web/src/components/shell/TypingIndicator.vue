<script setup lang="ts">
// TypingIndicator — the "X is typing…" line just above the composer (ENG-128).
//
// Subscribes to the worker's EPHEMERAL `{kind:'typing', stream_id}` push for the
// currently-open stream (ENG-126 seam): re-subscribes on stream change and
// unsubscribes on unmount so no leaked callbacks outlive the view. The worker
// TTL-expires stale typers (~5s) and pushes the shrunken set, so this component
// holds no timers of its own. Nothing is persisted — memory-only, like presence.
//
// Wording: the set EXCLUDES the signed-in user (your own typing is not news),
// ids resolve to display names via the same directory map the message list uses
// (raw-id fallback), and the line reads "{Name} is typing…" for one,
// "{A} and {B} are typing…" for two, "Several people are typing…" for 3+.
// The line lives in a FIXED-HEIGHT strip so the composer never jumps.
//
// SECURITY: display names are other users' input — rendered via Vue text
// interpolation only (never v-html).
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { resolveWorkerClient } from '../../composables/useWorkerClient'
import type { Unsubscribe } from '../../worker'

const props = withDefaults(
  defineProps<{
    /** The open stream to listen on; null/undefined renders an idle empty strip. */
    streamId?: string | null
    /** Directory `user_id → display_name` map (raw-id fallback when absent). */
    names?: ReadonlyMap<string, string> | undefined
    /** The signed-in user's id — excluded from the rendered set. */
    myUserId?: string | undefined
  }>(),
  { streamId: null, names: undefined, myUserId: undefined },
)

/** The stream's current (non-expired) typing user set, as last pushed. */
const typingIds = ref<string[]>([])
let unsub: Unsubscribe | undefined
/** Guards the async subscribe against a stream change racing the client resolve. */
let generation = 0

async function resubscribe(streamId: string | null): Promise<void> {
  const gen = ++generation
  unsub?.()
  unsub = undefined
  typingIds.value = []
  if (streamId === null) return
  const client = await resolveWorkerClient()
  // The stream changed (or we unmounted) while the client was resolving —
  // subscribing now would leak a callback bound to a stale stream.
  if (gen !== generation) return
  unsub = client.typing.subscribe(streamId, (payload) => {
    if (payload.stream_id !== streamId) return
    typingIds.value = payload.user_ids
  })
}

watch(
  () => props.streamId,
  (streamId) => {
    void resubscribe(streamId ?? null)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  generation++
  unsub?.()
  unsub = undefined
})

/** Everyone typing EXCEPT the signed-in user. */
const others = computed(() => typingIds.value.filter((id) => id !== props.myUserId))

function nameOf(id: string): string {
  return props.names?.get(id) ?? id
}

const label = computed(() => {
  const ids = others.value
  if (ids.length === 0) return ''
  if (ids.length === 1) return `${nameOf(ids[0]!)} is typing…`
  if (ids.length === 2) return `${nameOf(ids[0]!)} and ${nameOf(ids[1]!)} are typing…`
  return 'Several people are typing…'
})
</script>

<template>
  <!-- Fixed-height strip (blends into the message-list background) so the
       composer never jumps when typing starts/stops. -->
  <div class="flex h-5 shrink-0 items-center bg-background px-4" aria-live="polite">
    <span v-if="label" class="animate-pulse text-xs text-muted" data-testid="typing-indicator">
      {{ label }}
    </span>
  </div>
</template>
