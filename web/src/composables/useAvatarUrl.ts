// composables/useAvatarUrl.ts — turn a member's worker-held avatar bytes into a
// renderable object URL (ENG-152), the avatar sibling of useFileUrl. The TAB
// requests the opaque `Blob` once through the WorkerClient (`client.users.avatar`),
// `URL.createObjectURL`s it, and `revokeObjectURL`s it when the last user unmounts.
//
// Same boundary reasoning as useFileUrl: a `blob:` URL is scoped to the realm
// that created it, so the tab mints it; only bytes cross the RPC boundary and the
// session token stays worker-side.
//
// One deliberate difference from useFileUrl: the source is REACTIVE. A user's
// avatar can change (or clear) while a component is mounted — the directory fold
// updates `avatar_sha256` — so this takes a getter and re-acquires on any
// userId/sha change, keyed `userId:sha` (a new sha is a new key → a fresh fetch;
// the shared refcount map still dedupes across every component showing the same
// face). `sha === undefined` means "no avatar": the url resolves to null and no
// worker call is made (so initials-only renders never touch the worker).

import { onScopeDispose, ref, watch, type Ref } from 'vue'

import { resolveWorkerClient } from './useWorkerClient'

/** A shared, refcounted object URL for one `userId:sha`. */
interface UrlEntry {
  refcount: number
  /** Resolves to the created object URL, or `null` on a 404 / unfetchable avatar. */
  promise: Promise<string | null>
}

const entries = new Map<string, UrlEntry>()

/** Acquire (creating on first use) the shared URL for `key`, bumping its refcount. */
function acquire(key: string, loadBlob: () => Promise<Blob | null>): Promise<string | null> {
  let entry = entries.get(key)
  if (!entry) {
    entry = {
      refcount: 0,
      // Reject-proof (useFileUrl precedent): a rejection folds to null so it can
      // never become an unhandled rejection nor leak a URL.
      promise: loadBlob()
        .then((blob) => (blob ? URL.createObjectURL(blob) : null))
        .catch(() => null),
    }
    entries.set(key, entry)
  }
  entry.refcount++
  return entry.promise
}

/** Release one hold on `key`; when the last releases, revoke + drop the URL. */
function release(key: string): void {
  const entry = entries.get(key)
  if (!entry) return
  entry.refcount--
  if (entry.refcount > 0) return
  entries.delete(key)
  void entry.promise.then((url) => {
    if (url) URL.revokeObjectURL(url)
  })
}

/**
 * Resolve a member's avatar to a `blob:` URL for `<img src>`. `source` is a
 * getter of the current `{ userId, sha }` (both from the directory record);
 * the returned `url` ref is `null` until loaded, and returns to `null` when the
 * avatar clears. Shared + refcounted across callers; revoked when the last
 * scope using a given `userId:sha` unmounts or moves off it.
 */
export function useAvatarUrl(
  source: () => { userId: string | undefined; sha: string | undefined },
): {
  url: Ref<string | null>
} {
  const url = ref<string | null>(null)
  let heldKey: string | null = null

  watch(
    source,
    ({ userId, sha }) => {
      const key = userId && sha ? `${userId}:${sha}` : null
      if (key === heldKey) return
      if (heldKey !== null) release(heldKey)
      heldKey = key
      if (key === null || userId === undefined || sha === undefined) {
        url.value = null
        return
      }
      url.value = null
      void acquire(key, async () => {
        const client = await resolveWorkerClient()
        const result = await client.users.avatar(userId, sha)
        return result.blob
      }).then((resolved) => {
        // Ignore a stale resolution if the source moved on meanwhile.
        if (heldKey === key) url.value = resolved
      })
    },
    { immediate: true },
  )

  onScopeDispose(() => {
    if (heldKey !== null) release(heldKey)
    heldKey = null
  })

  return { url }
}
