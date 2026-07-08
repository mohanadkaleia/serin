<script setup lang="ts">
// ShellView — the authed app shell (ENG-82, TDD §5.4; restructured in ENG-136
// "Ranin" PR-B). It composes the Ranin three-column layout — SpaceRail | feed-first
// AppSidebar | main (channel-header + virtualized MessageList + MessageComposer, OR
// a scaffold EmptyState) | RightDrawer (thread) — over the worker RPC, and delegates
// ALL cross-store wiring to `useShellController` so PR-C's AppShell swap is a pure
// reshuffle with identical behavior. No message data ever comes from the HTTP API —
// the shell reads exclusively through the worker client (via the stores).
//
// STILL LIGHT theme (dark ships in PR-D); the message/composer components are NOT
// restyled here (that's PR-D too) — this PR only restructures the shell chrome.
import AppSidebar from '../components/shell/AppSidebar.vue'
import CommandPalette from '../components/shell/CommandPalette.vue'
import MessageComposer from '../components/shell/MessageComposer.vue'
import MessageList from '../components/shell/MessageList.vue'
import RightDrawer from '../components/shell/RightDrawer.vue'
import SpaceRail from '../components/shell/SpaceRail.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import { useShellController } from '../composables/useShellController'

const {
  messages,
  activeView,
  paletteOpen,
  editingMessageId,
  canAdmin,
  workspaceName,
  workspaceInitials,
  selectedStream,
  selectedStreamId,
  mentionItems,
  displayMessages,
  hasMore,
  threadOpen,
  mainTitle,
  scaffold,
  composerPlaceholder,
  quickItems,
  setActiveView,
  openPalette,
  onPaletteSelect,
  onSend,
  onEditLast,
  onReact,
  onEditStart,
  onEditSubmit,
  onEditCancel,
  onDeleteMessage,
  onOpenThread,
  onLogout,
} = useShellController()
</script>

<template>
  <div class="flex h-screen w-screen overflow-hidden bg-background text-primary">
    <SpaceRail
      :workspace-initials="workspaceInitials"
      :workspace-name="workspaceName"
      @logout="onLogout"
    />

    <AppSidebar
      :active-view="activeView"
      :workspace-name="workspaceName"
      :can-admin="canAdmin"
      @open-switcher="openPalette"
      @select-view="setActiveView"
    />

    <main class="flex min-w-0 flex-1 flex-col">
      <header
        class="flex items-center justify-between border-b border-subtle px-4 py-3"
        data-testid="channel-header"
      >
        <h1 class="truncate text-sm font-semibold text-primary">{{ mainTitle }}</h1>
      </header>

      <!-- Live conversation timeline (real channel/DM). -->
      <template v-if="activeView === 'conversation'">
        <MessageList
          :messages="displayMessages"
          :has-more="hasMore"
          :stream-key="selectedStreamId"
          :load-older="messages.loadOlder"
          :editing-message-id="editingMessageId"
          @retry="messages.retry"
          @discard="messages.discard"
          @react="onReact"
          @edit-start="onEditStart"
          @edit-submit="onEditSubmit"
          @edit-cancel="onEditCancel"
          @delete="onDeleteMessage"
          @open-thread="onOpenThread"
        />

        <MessageComposer
          :placeholder="composerPlaceholder"
          :disabled="!selectedStream"
          :mention-items="mentionItems"
          :stream-id="selectedStreamId ?? undefined"
          @send="onSend"
          @edit-last="onEditLast"
        />
      </template>

      <!-- Scaffold placeholder (Inbox / Feeds / Apps / Admin). -->
      <div v-else class="flex flex-1 items-center justify-center">
        <EmptyState v-if="scaffold" :title="scaffold.title" :description="scaffold.body" />
      </div>
    </main>

    <!-- M3 thread pane (ENG-103) — hosted in the Ranin right drawer. -->
    <RightDrawer :open="threadOpen" />

    <CommandPalette
      :open="paletteOpen"
      :items="quickItems"
      @select="onPaletteSelect"
      @close="paletteOpen = false"
    />
  </div>
</template>
