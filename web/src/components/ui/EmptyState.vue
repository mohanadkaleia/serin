<script setup lang="ts">
// ui/EmptyState.vue — ENG-136 "Ranin" primitive (PR-A). A centered empty/placeholder
// block: an optional icon slot, a title, and a muted description. Used for the
// scaffold sidebar sections in PR-B/C.
withDefaults(defineProps<{ title: string; description?: string }>(), { description: '' })
</script>

<template>
  <div class="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
    <span v-if="$slots.icon" class="text-muted">
      <slot name="icon" />
    </span>
    <p class="text-[13px] font-medium text-primary">{{ title }}</p>
    <p v-if="description" class="max-w-xs text-[12px] text-muted">{{ description }}</p>
    <div v-if="$slots.action" class="mt-1">
      <slot name="action" />
    </div>
  </div>
</template>
