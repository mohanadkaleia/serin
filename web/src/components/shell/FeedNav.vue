<script setup lang="ts">
// FeedNav — ENG-136 "Ranin" feed-first navigation (PR-3). The expandable "Feeds"
// entry: a top row (leading `rss` icon + label + a chevron that toggles the body)
// with the feed sub-streams indented beneath — Mentions, Tagged in, Subscribed, My
// channels, App alerts, Saved, Drafts, each with a small count.
//
// SCAFFOLD: the whole section is a stand-in — the sub-streams and their counts are
// static placeholders; clicking ANY of them (or the header label) flips the main
// panel to the Feeds placeholder view. When a real feeds projection lands, only the
// data source here changes; the layout stays.
import { ref } from 'vue'

import Icon from '../ui/Icon.vue'
import SidebarItem from '../ui/SidebarItem.vue'

defineProps<{ active: boolean }>()
const emit = defineEmits<{ selectView: [] }>()

/** SCAFFOLD sub-streams with placeholder counts (0 = no badge shown). */
const SUB_ITEMS: ReadonlyArray<{ id: string; label: string; count: number }> = [
  { id: 'mentions', label: 'Mentions', count: 3 },
  { id: 'tagged', label: 'Tagged in', count: 1 },
  { id: 'subscribed', label: 'Subscribed', count: 0 },
  { id: 'my-channels', label: 'My channels', count: 0 },
  { id: 'app-alerts', label: 'App alerts', count: 5 },
  { id: 'saved', label: 'Saved', count: 0 },
  { id: 'drafts', label: 'Drafts', count: 2 },
]

const open = ref(false)
function toggle(): void {
  open.value = !open.value
}
</script>

<template>
  <section>
    <div class="flex items-center">
      <SidebarItem :active="active" data-testid="nav-feeds" @click="emit('selectView')">
        <template #leading><Icon name="rss" :size="16" /></template>
        Feeds
      </SidebarItem>
      <button
        type="button"
        class="ml-1 grid h-7 w-5 shrink-0 place-items-center rounded text-muted transition-colors hover:text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        :aria-expanded="open"
        aria-label="Toggle feed sub-streams"
        data-testid="nav-feeds-toggle"
        @click="toggle"
      >
        <Icon name="chevron-down" :size="14" :class="open ? '' : '-rotate-90'" />
      </button>
    </div>

    <div v-show="open" class="mt-0.5 space-y-px pl-4">
      <SidebarItem
        v-for="item in SUB_ITEMS"
        :key="item.id"
        :active="active"
        data-testid="feed-subitem"
        :data-feed="item.id"
        @click="emit('selectView')"
      >
        {{ item.label }}
        <template v-if="item.count > 0" #trailing>
          <span class="rounded-full bg-accent-subtle px-1.5 text-xs font-medium text-accent">{{
            item.count
          }}</span>
        </template>
      </SidebarItem>
    </div>
  </section>
</template>
