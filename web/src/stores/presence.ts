// stores/presence.ts — tab-side mirror of the ENG-126 ephemeral presence snapshot.
//
// Presence is workspace-wide `user_id → online|offline`, seeded and updated from
// the worker's `{kind:'presence'}` push (the FULL current snapshot each time). It
// is EPHEMERAL: memory-only, never persisted — the worker owns the source of truth
// and re-derives it from live WS frames on every (re)connect. This store adds NO
// logic of its own; it caches the last snapshot and exposes lookups for the UI.
//
// The signed-in user is treated as ONLINE by default (you are online by definition
// while your tab is connected); OTHER users default to `offline` until a frame says
// otherwise. So the UserCard dot reads "online" the moment the shell mounts.

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { resolveWorkerClient } from '../composables/useWorkerClient'
import type { PresenceStatus, Unsubscribe } from '../worker'

export const usePresenceStore = defineStore('presence', () => {
  /** Last presence snapshot as `user_id → status` (replaced whole on each push). */
  const statuses = ref<Map<string, PresenceStatus>>(new Map())
  /** The signed-in user's id, so `myStatus` can resolve without a second store. */
  const myUserId = ref<string | undefined>(undefined)
  let unsub: Unsubscribe | undefined

  /** Subscribe to the presence push (seeds from the in-memory snapshot). Idempotent. */
  async function start(userId?: string): Promise<void> {
    myUserId.value = userId
    const client = await resolveWorkerClient()
    if (!unsub) {
      unsub = client.presence.subscribe((payload) => {
        const next = new Map<string, PresenceStatus>()
        for (const entry of payload.presence) next.set(entry.user_id, entry.status)
        statuses.value = next
      })
    }
  }

  function stop(): void {
    unsub?.()
    unsub = undefined
    statuses.value = new Map()
  }

  /** A user's presence — `offline` when unknown (no frame seen for them yet). */
  function statusOf(userId: string): PresenceStatus {
    return statuses.value.get(userId) ?? 'offline'
  }

  /** The signed-in user's presence — defaults to `online` (connected ⇒ online). */
  const myStatus = computed<PresenceStatus>(() => {
    const id = myUserId.value
    if (id === undefined) return 'online'
    return statuses.value.get(id) ?? 'online'
  })

  return { statuses, myUserId, start, stop, statusOf, myStatus }
})
