<script setup lang="ts">
// ui/IconButton.vue — ENG-136 "Ranin" primitive (PR-A). A square, icon-only button.
// `aria-label` is REQUIRED (icon-only controls are otherwise unlabeled) and we
// fail loudly in dev if it's missing/blank. Focus-visible accent ring; hover
// surface. Icon goes in the default slot (inline SVG, currentColor).
import { computed } from 'vue'

type Size = 'sm' | 'md'

const props = withDefaults(
  defineProps<{
    /** REQUIRED accessible name — icon-only buttons have no text. */
    ariaLabel: string
    size?: Size
    type?: 'button' | 'submit' | 'reset'
    disabled?: boolean
  }>(),
  { size: 'md', type: 'button', disabled: false },
)

// Dev-only guard: an icon button with no accessible name is a bug, not a warning.
if (import.meta.env.DEV && (!props.ariaLabel || props.ariaLabel.trim() === '')) {
  throw new Error('[ui/IconButton] `aria-label` is required for an icon-only button.')
}

const base =
  'inline-flex items-center justify-center rounded text-secondary transition-colors ' +
  'hover:bg-surface hover:text-primary focus:outline-none focus-visible:ring-2 ' +
  'focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-background ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

const sizeClasses: Record<Size, string> = {
  sm: 'h-6 w-6',
  md: 'h-7 w-7',
}

const classes = computed(() => [base, sizeClasses[props.size]])
</script>

<template>
  <button :type="type" :disabled="disabled" :aria-label="ariaLabel" :class="classes">
    <slot />
  </button>
</template>
