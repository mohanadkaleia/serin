<script setup lang="ts">
// RightDrawer — ENG-136 "Ranin" right drawer (PR-B). A thin wrapper that hosts the
// thread panel as a right-hand column. It replaces the old conditional
// `<aside w-96><ThreadPane/></aside>` in ShellView: SAME open/close behavior
// (synchronous mount/unmount — no leave transition, so closing removes the pane
// immediately, exactly as before), and it reuses <ThreadPane> VERBATIM inside — so
// the `thread-pane` / `thread-close` / `thread-composer` testids (which live in
// ThreadPane) are unchanged. A mount-only CSS entrance gives the open a subtle
// slide-in without ever delaying the close. Landmark: role="complementary",
// aria-label="Thread".
import ThreadPane from './ThreadPane.vue'

defineProps<{ open: boolean }>()
</script>

<template>
  <div
    v-if="open"
    role="complementary"
    aria-label="Thread"
    class="drawer-panel h-full w-96 shrink-0 overflow-hidden"
  >
    <ThreadPane />
  </div>
</template>

<style scoped>
@keyframes drawer-in {
  from {
    opacity: 0;
    transform: translateX(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.drawer-panel {
  animation: drawer-in 150ms ease-out;
}
@media (prefers-reduced-motion: reduce) {
  .drawer-panel {
    animation: none;
  }
}
</style>
