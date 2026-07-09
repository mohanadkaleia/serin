<script setup lang="ts">
// ProfileDialog — the current user's own profile (view + edit display name).
//
// Reachable from the sidebar footer UserCard (the `open-profile` affordance). It
// reads/writes ONLY through the `client.me.*` worker RPCs (the worker owns the
// token; nothing here touches HTTP or an API path — `no-http-in-ui` stays
// green). The authoritative email/role come from `me.get`; presence is the live
// ephemeral store snapshot; the display name is the ONE editable field.
//
// After a successful save the server appends `user.profile_updated` to
// workspace-meta, which the sync engine folds into the local directory — so the
// UserCard + author names pick up the new name. We nudge that along by refreshing
// the workspace directory once the save resolves (best-effort; the fold is the
// source of truth either way).
//
// SECURITY: display name / email are user-controlled — rendered via text
// interpolation only.
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'

import { resolveWorkerClient } from '../../composables/useWorkerClient'
import { usePresenceStore } from '../../stores/presence'
import { useWorkspaceStore } from '../../stores/workspace'
import Button from '../ui/Button.vue'
import PresenceDot from '../ui/PresenceDot.vue'

import type { MeProfile } from '../../worker'

const emit = defineEmits<{ close: [] }>()

const presence = usePresenceStore()
const workspace = useWorkspaceStore()
const { myStatus } = storeToRefs(presence)

/** The loaded profile (email/role/is_bot are read-only; display name is edited). */
const profile = ref<MeProfile | null>(null)
/** The editable display-name draft, seeded from the loaded profile. */
const draft = ref('')
const loading = ref(true)
/** A failed initial LOAD (renders a retryable error state). */
const loadError = ref<string | null>(null)
/** A failed SAVE (inline error under the field). */
const saveError = ref<string | null>(null)
/** True while a save RPC is in flight. */
const saving = ref(false)
/** True briefly after a successful save (the "Saved" confirmation). */
const saved = ref(false)

/** problem `code` → field-level copy; unmapped codes fall back to a generic line. */
const SAVE_MESSAGES: Record<string, string> = {
  'validation-error': 'Enter a name between 1 and 200 characters.',
  unauthenticated: 'Your session has expired. Please sign in again.',
  network: 'Could not reach the server. Check your connection and try again.',
}

/** The coded slug off an RPC rejection (worker `RpcCodedError` → tab `RpcCallError`). */
function errorCode(err: unknown): string | null {
  if (err !== null && typeof err === 'object' && 'code' in err) {
    const { code } = err
    if (typeof code === 'string') return code
  }
  return null
}

function saveErrorCopy(err: unknown): string {
  const code = errorCode(err)
  return (
    (code !== null ? SAVE_MESSAGES[code] : undefined) ??
    'Could not save your profile. Please try again.'
  )
}

const initial = computed(() => (draft.value.trim()[0] ?? '?').toUpperCase())
const statusLabel = computed(() => (myStatus.value === 'online' ? 'Online' : 'Offline'))

/** Save is offered only for a non-empty, in-bounds, CHANGED name. */
const canSave = computed(() => {
  const trimmed = draft.value.trim()
  return (
    !saving.value &&
    trimmed.length > 0 &&
    trimmed.length <= 200 &&
    trimmed !== (profile.value?.display_name ?? '')
  )
})

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const client = await resolveWorkerClient()
    const me = await client.me.get()
    profile.value = me
    draft.value = me.display_name
  } catch {
    loadError.value = 'Could not load your profile.'
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!canSave.value) return
  saving.value = true
  saveError.value = null
  saved.value = false
  try {
    const client = await resolveWorkerClient()
    const updated = await client.me.update({ display_name: draft.value.trim() })
    profile.value = updated
    draft.value = updated.display_name
    saved.value = true
    // Nudge the directory fold so the footer UserCard reflects the new name once
    // the `user.profile_updated` event has synced (best-effort; not load-bearing).
    void workspace.refresh()
  } catch (err) {
    saveError.value = saveErrorCopy(err)
  } finally {
    saving.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    role="dialog"
    aria-modal="true"
    aria-label="Your profile"
    data-testid="profile-dialog"
    @click.self="emit('close')"
  >
    <div class="w-full max-w-sm rounded-lg border border-subtle bg-surface-elevated p-5 shadow-xl">
      <h2 class="mb-4 text-sm font-semibold text-primary">Your profile</h2>

      <p
        v-if="loading"
        class="py-6 text-center text-[13px] text-muted"
        data-testid="profile-loading"
      >
        Loading your profile…
      </p>

      <div v-else-if="loadError" data-testid="profile-load-error">
        <p class="mb-3 text-[13px] text-danger">{{ loadError }}</p>
        <Button variant="ghost" size="sm" data-testid="profile-retry" @click="load">Retry</Button>
      </div>

      <template v-else-if="profile">
        <!-- Identity header: initials avatar + presence dot + status label. -->
        <div class="mb-4 flex items-center gap-3">
          <span class="relative shrink-0">
            <span
              aria-hidden="true"
              class="grid h-12 w-12 place-items-center rounded-full bg-accent-subtle text-lg font-semibold text-accent"
              >{{ initial }}</span
            >
            <PresenceDot
              :status="myStatus"
              class="absolute -bottom-0.5 -right-0.5 border-2 border-surface-elevated"
            />
          </span>
          <div class="min-w-0">
            <p class="truncate text-sm font-medium text-primary" data-testid="profile-name-preview">
              {{ draft.trim() || profile.display_name }}
            </p>
            <p class="text-xs text-muted" data-testid="profile-presence">{{ statusLabel }}</p>
          </div>
        </div>

        <!-- Editable display name. -->
        <label class="mb-1 block text-xs font-medium text-secondary" for="profile-name-input"
          >Display name</label
        >
        <input
          id="profile-name-input"
          v-model="draft"
          type="text"
          maxlength="200"
          class="mb-1 w-full rounded-md border border-strong bg-transparent px-2 py-1.5 text-sm text-primary placeholder:text-muted focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-background"
          data-testid="profile-display-name"
          autocomplete="off"
          @keydown.enter.prevent="save"
        />
        <p v-if="saveError" class="mb-2 text-xs text-danger" data-testid="profile-error">
          {{ saveError }}
        </p>
        <p v-else-if="saved" class="mb-2 text-xs text-success" data-testid="profile-saved">
          Saved.
        </p>
        <div class="mb-4" />

        <!-- Read-only: email + role. -->
        <dl class="mb-5 space-y-2">
          <div class="flex items-center justify-between gap-3">
            <dt class="text-xs font-medium text-secondary">Email</dt>
            <dd class="truncate text-[13px] text-primary" data-testid="profile-email">
              {{ profile.email }}
            </dd>
          </div>
          <div class="flex items-center justify-between gap-3">
            <dt class="text-xs font-medium text-secondary">Role</dt>
            <dd>
              <span
                class="rounded-full border border-subtle px-1.5 text-[11px] font-medium capitalize text-secondary"
                data-testid="profile-role"
                >{{ profile.role }}</span
              >
            </dd>
          </div>
        </dl>

        <div class="flex justify-end gap-2">
          <Button variant="ghost" size="sm" data-testid="profile-close" @click="emit('close')">
            Close
          </Button>
          <Button
            variant="primary"
            size="sm"
            :disabled="!canSave"
            data-testid="profile-save"
            @click="save"
          >
            {{ saving ? 'Saving…' : 'Save' }}
          </Button>
        </div>
      </template>
    </div>
  </div>
</template>
