<script setup lang="ts">
// CommandPalette — the Cmd/Ctrl+K palette (ENG-82 quick-switcher, upgraded to a
// REAL command palette under ENG-136). One input, one unified keyboard-driven
// list with two GROUPS: "Commands" (registered actions — create channel, start
// DM, search, theme, sign out, …) and "Channels & DMs" (the original fuzzy
// stream navigation; DMs carry their REAL participant names, ENG-149).
// Open → focus, type → fuzzy-rank BOTH groups, ↑/↓ move across the whole list,
// Enter runs/navigates the highlighted row, Esc closes. Presentational only:
// commands arrive as display descriptors and `run` stays controller-side (the
// palette emits `run(id)`); stream selection emits `select(id)` as before.
// SECURITY: stream labels are other users' input — text interpolation only.
import { computed, nextTick, ref, watch } from 'vue'

import { fuzzyFilter, fuzzyScore } from '../../lib/fuzzy'
import Icon from '../ui/Icon.vue'

import type { IconName } from '../ui/Icon.vue'

/** One switchable target: a channel or DM. */
export interface QuickItem {
  id: string
  label: string
  kind: string
  unread: number
}

/** A command's DISPLAY shape (the registry's `run` stays with the controller). */
export interface CommandItem {
  id: string
  title: string
  icon: IconName
  keywords?: string | undefined
}

/** How many commands the empty-query view surfaces (streams stay unbounded). */
const EMPTY_QUERY_COMMANDS = 5

const props = defineProps<{ open: boolean; items: QuickItem[]; commands: CommandItem[] }>()
const emit = defineEmits<{ select: [id: string]; run: [id: string]; close: [] }>()

const query = ref('')
const activeIndex = ref(0)
const input = ref<HTMLInputElement | null>(null)

/** Commands group: top few on an empty query, else fuzzy on title OR keywords. */
const commandResults = computed<CommandItem[]>(() => {
  if (query.value.trim() === '') return props.commands.slice(0, EMPTY_QUERY_COMMANDS)
  const ranked: Array<{ command: CommandItem; score: number }> = []
  for (const command of props.commands) {
    const byTitle = fuzzyScore(query.value, command.title)
    const byKeywords = command.keywords ? fuzzyScore(query.value, command.keywords) : null
    const score =
      byTitle !== null && byKeywords !== null
        ? Math.max(byTitle, byKeywords)
        : (byTitle ?? byKeywords)
    if (score !== null) ranked.push({ command, score })
  }
  ranked.sort((a, b) => b.score - a.score)
  return ranked.map((r) => r.command)
})

/** Navigation group: the original fuzzy stream filter (order = sidebar order). */
const streamResults = computed(() =>
  fuzzyFilter(props.items, query.value, (i) => i.label).map((m) => m.item),
)

/** Total row count — the ↑/↓ highlight moves across BOTH groups as one list. */
const total = computed(() => commandResults.value.length + streamResults.value.length)

// Reset + focus each time the palette opens.
watch(
  () => props.open,
  (open) => {
    if (open) {
      query.value = ''
      activeIndex.value = 0
      void nextTick(() => input.value?.focus())
    }
  },
)

// Keep the highlight within the (shrinking) result set as the query narrows.
watch(total, (n) => {
  if (activeIndex.value >= n) activeIndex.value = Math.max(0, n - 1)
})

function move(delta: number): void {
  const n = total.value
  if (n === 0) return
  activeIndex.value = (activeIndex.value + delta + n) % n
}

/** Run/navigate the row at a flat index (commands first, then streams). */
function choose(index: number): void {
  const command = commandResults.value[index]
  if (command) {
    emit('run', command.id)
    return
  }
  const item = streamResults.value[index - commandResults.value.length]
  if (item) emit('select', item.id)
}

function onKeydown(event: KeyboardEvent): void {
  switch (event.key) {
    case 'ArrowDown':
      event.preventDefault()
      move(1)
      break
    case 'ArrowUp':
      event.preventDefault()
      move(-1)
      break
    case 'Enter':
      event.preventDefault()
      choose(activeIndex.value)
      break
    case 'Escape':
      event.preventDefault()
      emit('close')
      break
  }
}

/** Single-letter avatar for a DM row (from its resolved participant label). */
function dmInitial(item: QuickItem): string {
  return item.label.trim()[0]?.toUpperCase() ?? '?'
}
</script>

<template>
  <div
    v-if="props.open"
    class="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh]"
    data-testid="command-palette"
    @click.self="emit('close')"
  >
    <div
      class="w-full max-w-lg overflow-hidden rounded-lg border border-subtle bg-surface-elevated shadow-xl"
    >
      <input
        ref="input"
        v-model="query"
        type="text"
        placeholder="Type a command or jump to a channel…"
        class="w-full border-b border-subtle bg-transparent px-4 py-3 text-sm text-primary outline-none placeholder:text-muted focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
        data-testid="command-palette-input"
        @keydown="onKeydown"
      />
      <ul class="max-h-80 overflow-y-auto py-1">
        <li v-if="total === 0" class="px-4 py-3 text-sm text-muted">No matches</li>

        <!-- Commands group (actions from the registry, fuzzy-filtered). -->
        <template v-if="commandResults.length > 0">
          <li class="px-4 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted">
            Commands
          </li>
          <li
            v-for="(command, index) in commandResults"
            :key="command.id"
            class="flex cursor-pointer items-center gap-2.5 px-4 py-2 text-sm"
            :class="index === activeIndex ? 'bg-accent-subtle text-primary' : 'text-secondary'"
            :data-testid="`palette-command-${command.id}`"
            :data-active="index === activeIndex"
            @click="choose(index)"
            @mousemove="activeIndex = index"
          >
            <Icon :name="command.icon" :size="16" class="shrink-0 text-muted" />
            <span class="truncate">{{ command.title }}</span>
          </li>
        </template>

        <!-- Navigation group (the original quick-switcher, fuzzy-filtered). -->
        <template v-if="streamResults.length > 0">
          <li class="px-4 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted">
            Channels &amp; DMs
          </li>
          <li
            v-for="(item, index) in streamResults"
            :key="item.id"
            class="flex cursor-pointer items-center gap-2.5 px-4 py-2 text-sm"
            :class="
              commandResults.length + index === activeIndex
                ? 'bg-accent-subtle text-primary'
                : 'text-secondary'
            "
            data-testid="command-palette-item"
            :data-active="commandResults.length + index === activeIndex"
            @click="choose(commandResults.length + index)"
            @mousemove="activeIndex = commandResults.length + index"
          >
            <!-- Leading glyph: '#' for a channel, an initial avatar for a DM. -->
            <span
              v-if="item.kind === 'dm'"
              class="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-accent-subtle text-[10px] font-semibold text-accent"
              >{{ dmInitial(item) }}</span
            >
            <Icon v-else name="hash" :size="16" class="shrink-0 text-muted" />
            <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
            <span
              v-if="item.unread > 0"
              class="ml-2 shrink-0 rounded-full bg-strong px-1.5 text-xs text-secondary"
              >{{ item.unread }}</span
            >
          </li>
        </template>
      </ul>
    </div>
  </div>
</template>
