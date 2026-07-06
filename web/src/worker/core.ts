// worker/core.ts — WorkerCore: ALL worker logic, transport-agnostic (D-3).
//
// It touches nothing but (1) an MsgDb handle and (2) a MessageSink. It never
// references `self`, ports, channels, or locks — that is what makes the sync
// engine (ENG-79) and projections (ENG-80), which register handlers here,
// unit-testable in vitest against fake-indexeddb or MemoryDb, no browser.

import { checkProjectionVersion } from './db'
import {
  MAX_CACHED_EVENTS_PER_STREAM,
  topicKey,
  type MessageSink,
  type MsgDb,
  type PushPayload,
  type RpcError,
  type RpcMethod,
  type RpcRequest,
  type ToWorker,
  type Topic,
} from './types'

/** A handler for one RPC method. ENG-79/80/81 register these via `register`. */
export type RpcHandler = (req: RpcRequest, clientId: string) => Promise<unknown>

interface Subscription {
  clientId: string
  topic: Topic
}

export class WorkerCore {
  private readonly handlers = new Map<RpcMethod, RpcHandler>()
  /** Keyed by the subscription's correlation id. */
  private readonly subs = new Map<string, Subscription>()
  private readonly clients = new Set<string>()

  constructor(
    private readonly db: MsgDb,
    private readonly sink: MessageSink,
  ) {
    this.registerDefaults()
  }

  /** Run once after construction: reconcile PROJECTION_VERSION (D-4). */
  async init(): Promise<void> {
    await checkProjectionVersion(this.db)
  }

  /** Extension point for ENG-79/80/81. Later registration wins. */
  register(method: RpcMethod, handler: RpcHandler): void {
    this.handlers.set(method, handler)
  }

  /** Route an inbound frame to the right handler and reply via the sink. */
  async handle(clientId: string, msg: ToWorker): Promise<void> {
    switch (msg.t) {
      case 'hello':
        this.clients.add(msg.clientId)
        return
      case 'bye':
        this.removeClient(msg.clientId)
        return
      case 'sub':
        this.subs.set(msg.id, { clientId, topic: msg.topic })
        this.sink(clientId, { t: 'res', id: msg.id, ok: true, result: { subscribed: true } })
        return
      case 'unsub':
        this.subs.delete(msg.id)
        this.sink(clientId, { t: 'res', id: msg.id, ok: true, result: { unsubscribed: true } })
        return
      case 'req': {
        const handler = this.handlers.get(msg.req.method)
        if (!handler) {
          this.sink(clientId, {
            t: 'res',
            id: msg.id,
            ok: false,
            error: { code: 'unknown_method', detail: msg.req.method },
          })
          return
        }
        try {
          const result = await handler(msg.req, clientId)
          this.sink(clientId, { t: 'res', id: msg.id, ok: true, result })
        } catch (err) {
          this.sink(clientId, { t: 'res', id: msg.id, ok: false, error: toRpcError(err) })
        }
        return
      }
    }
  }

  /** Fan a push out to every client subscribed to `topic`. */
  publish<T extends Topic>(topic: T, payload: PushPayload<T>): void {
    const key = topicKey(topic)
    for (const sub of this.subs.values()) {
      if (topicKey(sub.topic) === key) {
        this.sink(sub.clientId, { t: 'push', topic, payload })
      }
    }
  }

  /**
   * Bounded-cache eviction (D-6): keep the newest ~MAX events for a stream,
   * delete older. It queries `events` only — it has no `outbox` handle, so it
   * structurally cannot touch pending sends. Not wired into a hot path here
   * (no apply loop until ENG-79); ships proven-safe.
   */
  async evictStream(streamId: string): Promise<void> {
    const seqs = await this.db.listEventSequences(streamId) // ascending
    if (seqs.length <= MAX_CACHED_EVENTS_PER_STREAM) return
    const cutoff = seqs.length - MAX_CACHED_EVENTS_PER_STREAM
    const toDelete = seqs.slice(0, cutoff)
    await this.db.deleteEventsBySequence(streamId, toDelete)
  }

  /** Drop a disconnecting client and all its subscriptions. */
  private removeClient(clientId: string): void {
    this.clients.delete(clientId)
    for (const [id, sub] of this.subs) {
      if (sub.clientId === clientId) this.subs.delete(id)
    }
  }

  // -- ENG-77 stub handlers ------------------------------------------------
  // `ping` and `meta.get` are real (they prove the round trip end to end);
  // `query`/`mutate` are registered but report not_implemented until ENG-80/81.

  private registerDefaults(): void {
    this.register('ping', () => Promise.resolve({ pong: true }))

    this.register('meta.get', async (req) => {
      if (req.method !== 'meta.get') throw new Error('meta.get handler misrouted')
      const value = await this.db.metaGet(req.params.key)
      return { key: req.params.key, value }
    })

    this.register('query', (req) => {
      const detail = req.method === 'query' ? req.params.q : undefined
      return Promise.resolve({ code: 'not_implemented', detail })
    })

    this.register('mutate', (req) => {
      const detail = req.method === 'mutate' ? req.params.m : undefined
      return Promise.resolve({ code: 'not_implemented', detail })
    })
  }
}

function toRpcError(err: unknown): RpcError {
  return {
    code: 'handler_error',
    detail: err instanceof Error ? err.message : String(err),
  }
}
