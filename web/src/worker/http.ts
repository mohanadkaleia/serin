// worker/http.ts — the authed HTTP client (ENG-78, R2). A small injectable
// `fetch` wrapper: every ENG-79/81 server call reuses it. Transport-agnostic and
// mockable — no real network is needed to unit-test auth logic (the injected
// `fetchImpl` is the seam). It NEVER throws for an HTTP error; it returns a typed
// `ApiResult`. The only rejection surface (network) is folded into an ApiError.
//
// The token is attached here, worker-side, as `Authorization: Bearer <token>` and
// never leaves this realm (R1). Errors are parsed from RFC 9457 problem+json.

/** A parsed error, shaped from problem+json (or synthesized for opaque failures). */
export interface ApiError {
  status: number
  /** problem `type` slug, e.g. 'invalid-credentials' (tail of `/problems/<slug>`). */
  code: string
  title: string
  detail?: string
  /** Parsed from the `Retry-After` header on a 429. */
  retryAfter?: number
}

export type ApiResult<T> = { ok: true; value: T } | { ok: false; error: ApiError }

export interface HttpClient {
  /** POST a JSON body. `authed` defaults true; login/setup/accept-invite pass false. */
  post<T>(path: string, body: unknown, opts?: { authed?: boolean }): Promise<ApiResult<T>>
  /** Authed GET returning parsed JSON. */
  get<T>(path: string): Promise<ApiResult<T>>
  /** Authed DELETE expecting 204 No Content. */
  del(path: string): Promise<ApiResult<void>>
}

export interface HttpClientDeps {
  /** Default '' → relative `/v1/...` paths (served same-origin by FastAPI, §5.1). */
  baseUrl?: string
  /** Injected in tests; defaults to the platform `fetch`. */
  fetchImpl?: typeof fetch
  /** Worker-held token accessor (R1). Returns null when unauthenticated. */
  getToken: () => string | null
  /** Invoked on a 401 before the typed error is returned (session invalid → clear). */
  onUnauthorized: () => void | Promise<void>
}

/** Minimal problem+json shape the client reads (server: msgd/api/problems.py). */
interface ProblemDocument {
  type?: string
  title?: string
  detail?: string
  status?: number
}

export function createHttpClient(deps: HttpClientDeps): HttpClient {
  const baseUrl = deps.baseUrl ?? ''
  const rawFetch = deps.fetchImpl ?? globalThis.fetch
  // Wrap so we never trip an "Illegal invocation" when the platform `fetch`
  // relies on its receiver, and so the injected impl is called uniformly.
  const doFetch: typeof fetch = (input, init) => rawFetch(input, init)

  async function request<T>(
    method: 'GET' | 'POST' | 'DELETE',
    path: string,
    body: unknown,
    authed: boolean,
  ): Promise<ApiResult<T>> {
    const headers: Record<string, string> = {}
    const hasBody = body !== undefined
    if (hasBody) headers['Content-Type'] = 'application/json'
    if (authed) {
      const token = deps.getToken()
      if (token) headers['Authorization'] = `Bearer ${token}`
    }

    let res: Response
    try {
      res = await doFetch(baseUrl + path, {
        method,
        headers,
        ...(hasBody ? { body: JSON.stringify(body) } : {}),
      })
    } catch {
      // Only a network/fetch rejection reaches here — HTTP errors are Responses.
      return { ok: false, error: { status: 0, code: 'network', title: 'Network error' } }
    }

    // The single choke point that turns an expired/revoked session into a
    // re-login state for the whole app (R2). Runs before we return the error.
    if (res.status === 401) {
      await deps.onUnauthorized()
    }

    if (res.ok) {
      if (res.status === 204) return { ok: true, value: undefined as T }
      const text = await res.text()
      if (!text) return { ok: true, value: undefined as T }
      return { ok: true, value: JSON.parse(text) as T }
    }

    return { ok: false, error: await parseError(res) }
  }

  return {
    post: <T>(path: string, body: unknown, opts?: { authed?: boolean }) =>
      request<T>('POST', path, body, opts?.authed ?? true),
    get: <T>(path: string) => request<T>('GET', path, undefined, true),
    del: (path: string) => request<void>('DELETE', path, undefined, true),
  }
}

async function parseError(res: Response): Promise<ApiError> {
  const retryAfter = parseRetryAfter(res.headers.get('Retry-After'))
  const fallbackCode = `http-${res.status}`
  try {
    const data = (await res.json()) as ProblemDocument
    const type = typeof data.type === 'string' ? data.type : ''
    const slug = type.split('/').pop()
    const code = slug && slug.length > 0 ? slug : fallbackCode
    return {
      status: res.status,
      code,
      title: typeof data.title === 'string' ? data.title : 'Request failed',
      ...(typeof data.detail === 'string' ? { detail: data.detail } : {}),
      ...(retryAfter !== undefined ? { retryAfter } : {}),
    }
  } catch {
    // Non-JSON / opaque failure — synthesize a stable code from the status.
    return {
      status: res.status,
      code: fallbackCode,
      title: 'Request failed',
      ...(retryAfter !== undefined ? { retryAfter } : {}),
    }
  }
}

function parseRetryAfter(header: string | null): number | undefined {
  if (!header) return undefined
  const seconds = Number.parseInt(header, 10)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : undefined
}
