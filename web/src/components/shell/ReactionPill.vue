<script setup lang="ts">
// ReactionPill — one aggregated reaction chip (ENG-136 "Ranin" PR-2).
//
// Extracted verbatim (behavior + test-ids) from MessageItem's inline chip so the
// timeline and any future surface share ONE pill. A `rounded-full` chip showing the
// emoji + count; MINE is accent-tinted, others are neutral. The `reaction-chip`
// test-id and the who-reacted `reaction-tooltip` are preserved — the m3-messaging-
// core E2E asserts `reaction-chip` visibility.
//
// SECURITY: `emoji` is OPAQUE bytes (may carry control chars) and `display_names`
// are other users' content; BOTH render ONLY through Vue text interpolation — there
// is no v-html or raw-HTML sink here.
import type { ReactionAggregate } from '../../worker'

const props = defineProps<{
  chip: ReactionAggregate
  /** Reactions are only actionable on a live (non-deleted, settled) row. */
  disabled?: boolean
}>()

const emit = defineEmits<{
  /** Toggle YOUR reaction — `mine` is the caller's read of current membership. */
  toggle: [emoji: string, mine: boolean]
}>()
</script>

<template>
  <button
    type="button"
    class="group/chip relative inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-background"
    :class="
      props.chip.mine
        ? 'border-accent bg-accent-subtle text-accent'
        : 'border-subtle bg-surface text-secondary hover:bg-surface-elevated'
    "
    data-testid="reaction-chip"
    :data-mine="props.chip.mine"
    :disabled="props.disabled"
    @click="emit('toggle', props.chip.emoji, props.chip.mine)"
  >
    <!-- OPAQUE emoji bytes — text interpolation only. -->
    <span>{{ props.chip.emoji }}</span>
    <span class="tabular-nums">{{ props.chip.count }}</span>
    <!-- Who-reacted tooltip: display names via interpolation (escaped). -->
    <span
      class="pointer-events-none absolute bottom-full left-0 z-20 mb-1 hidden whitespace-nowrap rounded border border-subtle bg-surface-elevated px-2 py-1 text-[11px] text-primary shadow-sm group-hover/chip:block"
      data-testid="reaction-tooltip"
      >{{ props.chip.display_names.join(', ') }}</span
    >
  </button>
</template>
