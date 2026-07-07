"""``httpx``-based HTTP client for the msg server (ENG-70 §1).

``msgctl`` remote mode is a genuine HTTP client: bearer-authenticated batch
uploads with **timeouts and a retry loop**, GET with query params, and RFC 9457
problem+json error bodies to decode. :class:`MsgClient` wraps a synchronous
``httpx.Client`` (msgctl is a sequential CLI — no asyncio) with:

* **Bearer injection** — the token is sent as ``Authorization: Bearer <token>``
  and is **never** logged, printed, or embedded in any exception message. The
  error path decodes only the response's problem+json body (which never contains
  the token) and never dumps request headers.
* **Explicit timeouts** — a connect + read timeout, one client reused per command.
* **Bounded retry on transient faults** — network errors, timeouts, HTTP 5xx and
  429 are retried with bounded exponential backoff (:meth:`_request`). A 4xx that
  is not 429 is a *permanent* fault, mapped immediately to a typed
  :class:`RemoteError` (no retry). This is the "idempotent dumb retry loop": a
  retried batch re-sends the same ``event_id``s and server idempotency
  (``UNIQUE(workspace_id, event_id)``) returns the original record — no
  duplicate.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from msgctl.errors import MsgctlError

__all__ = [
    "RemoteError",
    "AuthError",
    "NotFoundError",
    "TransientError",
    "ProtocolError",
    "MsgClient",
]

#: Default per-request timeouts (seconds). A generous read timeout tolerates a
#: cold server; connect stays short so an unreachable host fails fast (then the
#: retry loop decides whether to back off and retry).
_DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

#: HTTP statuses that are transient (retryable): 429 + all 5xx.
_RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

#: Loopback hosts for which cleartext ``http://`` is acceptable (local dev).
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})


def _warn_cleartext(server_url: str) -> None:
    """Warn (once, on stderr) if the bearer token would ride cleartext ``http://``.

    Rule (M1): warn — do not reject — since local dev legitimately uses
    ``http://localhost``. A non-loopback ``http://`` host sends the bearer token
    in the clear, so the operator is told to use ``https``. Never rejects (that
    would block localhost dev over http). The URL host is shown; the token is not.
    """
    parts = urlsplit(server_url)
    if parts.scheme == "http" and (parts.hostname or "") not in _LOOPBACK_HOSTS:
        print(
            f"warning: server URL is http://{parts.netloc} — the bearer token is sent in "
            "cleartext; use https",
            file=sys.stderr,
        )


class RemoteError(MsgctlError):
    """A server/transport error surfaced to the operator (exit 1).

    Never carries the bearer token or request headers — only the HTTP status, the
    problem+json ``type``/``detail``, and the request path.
    """

    def __init__(self, message: str, *, status: int | None = None, type: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.type = type


class AuthError(RemoteError):
    """401/403 — the session token is missing, invalid, or unauthorized."""


class NotFoundError(RemoteError):
    """404 — the resource (or a non-disclosed private stream) does not exist."""


class TransientError(RemoteError):
    """A transient fault survived the whole retry budget (network/timeout/5xx/429)."""


class ProtocolError(RemoteError):
    """The server sent a structurally invalid / hostile response (e.g. a bad id).

    Raised BEFORE any server-supplied value is trusted as a filesystem path, so a
    malicious server cannot drive path traversal. The message names only the
    offending value's *shape*, never the raw string (which could itself be a
    traversal payload echoed into a log/path).
    """


#: Defensive caps on the per-field 422 summary so a hostile/buggy server cannot
#: inflate an error message: surface at most this many field errors, each ``msg``
#: truncated to this many characters.
_MAX_FIELD_ERRORS: Final = 5
_MAX_FIELD_MSG_LEN: Final = 200


def _summarize_field_errors(errors: object) -> str:
    """Compact ``(field: msg; field2: msg2)`` from a problem+json ``errors`` list.

    Consumes the server's sanitized ``errors`` extension member — a list of
    ``{loc, msg, type}`` items with ``input``/``ctx`` already dropped server-side.
    Only ``loc`` (last element → field name) and ``msg`` are surfaced; no submitted
    value is ever read (there is none in the item to begin with). Returns ``""``
    when there is nothing usable. Never raises — malformed items are skipped.
    """
    if not isinstance(errors, list):
        return ""
    parts: list[str] = []
    for err in errors:
        if len(parts) >= _MAX_FIELD_ERRORS:
            break
        if not isinstance(err, dict):
            continue
        loc = err.get("loc")
        field = str(loc[-1]) if isinstance(loc, list) and loc else "?"
        msg = str(err.get("msg", "")).strip()
        if not msg:
            continue
        if len(msg) > _MAX_FIELD_MSG_LEN:
            msg = msg[:_MAX_FIELD_MSG_LEN] + "…"
        parts.append(f"{field}: {msg}")
    if not parts:
        return ""
    return " (" + "; ".join(parts) + ")"


def _problem_fields(resp: httpx.Response) -> tuple[str | None, str]:
    """Best-effort ``(type, detail)`` from a problem+json body; never raises.

    Falls back to the reason phrase when the body is not JSON. The token is not
    in the response body, so this is safe to surface. When the body carries a
    validation ``errors`` list (422s), a compact per-field summary is appended to
    ``detail`` so the operator sees *which* field failed and why (e.g. a too-short
    password) instead of the generic "one or more fields are invalid".
    """
    try:
        data = resp.json()
    except ValueError:
        return None, resp.reason_phrase or f"HTTP {resp.status_code}"
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("title") or resp.reason_phrase
        type_ = data.get("type")
        summary = _summarize_field_errors(data.get("errors"))
        return (type_ if isinstance(type_, str) else None), f"{detail}{summary}"
    return None, resp.reason_phrase or f"HTTP {resp.status_code}"


class MsgClient:
    """A synchronous, bearer-authenticated client for one msg server.

    One instance is built per command and reused across its requests (connection
    pooling). ``token`` may be ``None`` for the unauthenticated endpoints
    (``setup``, ``accept-invite``); the authenticated methods require it.
    """

    def __init__(
        self,
        server_url: str,
        *,
        token: str | None = None,
        max_retries: int = 5,
        backoff_base: float = 0.2,
        timeout: httpx.Timeout | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        _warn_cleartext(server_url)
        # ``transport`` is an injection seam for unit tests (httpx.MockTransport);
        # production passes None and httpx builds the default networking transport.
        self._client = httpx.Client(
            base_url=server_url.rstrip("/"),
            timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
            transport=transport,
        )

    def __enter__(self) -> MsgClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- core request/retry -------------------------------------------------

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if auth:
            if self._token is None:
                raise AuthError("no bearer token available; run `msgctl login`")
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _sleep(self, attempt: int) -> None:
        # Bounded exponential backoff; a backoff_base of 0 (tests) makes it instant.
        if self._backoff_base > 0:
            time.sleep(self._backoff_base * (2**attempt))

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one request with the transient-fault retry loop; return the JSON object.

        Transient faults (transport errors, timeouts, 5xx, 429) are retried up to
        ``max_retries`` with bounded backoff; the SAME request is re-sent, so
        server idempotency makes a retried batch safe. A permanent 4xx is mapped
        to a typed :class:`RemoteError` on the first response (no retry). Every
        msg endpoint answers with a JSON object, so a non-object body is itself a
        :class:`RemoteError`.
        """
        headers = self._headers(auth=auth)
        last_transient: str = "request failed"
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.request(
                    method, path, headers=headers, json=json_body, params=params
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # Network/timeout: transient. Retry (never leaks headers/token —
                # only the exception class name, no request context).
                last_transient = f"{type(exc).__name__} contacting {path}"
                if attempt < self._max_retries:
                    self._sleep(attempt)
                    continue
                raise TransientError(f"{last_transient} (after {attempt + 1} attempts)") from exc

            if resp.status_code in _RETRY_STATUSES:
                type_, detail = _problem_fields(resp)
                last_transient = f"HTTP {resp.status_code} on {path}: {detail}"
                if attempt < self._max_retries:
                    self._sleep(attempt)
                    continue
                raise TransientError(
                    f"{last_transient} (after {attempt + 1} attempts)",
                    status=resp.status_code,
                    type=type_,
                )

            if resp.status_code >= 400:
                self._raise_for_status(resp, path)

            try:
                data = resp.json()
            except ValueError as exc:
                raise RemoteError(f"non-JSON response from {path}") from exc
            if not isinstance(data, dict):
                raise RemoteError(f"expected a JSON object from {path}")
            return data

        # Unreachable: the loop either returns or raises on the final attempt.
        raise TransientError(last_transient)  # pragma: no cover

    @staticmethod
    def _raise_for_status(resp: httpx.Response, path: str) -> None:
        """Map a permanent 4xx response to a typed error (no token/header leak)."""
        type_, detail = _problem_fields(resp)
        message = f"HTTP {resp.status_code} on {path}: {detail}"
        if resp.status_code in (401, 403):
            raise AuthError(message, status=resp.status_code, type=type_)
        if resp.status_code == 404:
            raise NotFoundError(message, status=resp.status_code, type=type_)
        raise RemoteError(message, status=resp.status_code, type=type_)

    # --- auth / identity endpoints ------------------------------------------

    def setup(
        self, *, workspace_name: str, email: str, password: str, display_name: str
    ) -> dict[str, Any]:
        """POST /v1/setup — first-run: create workspace + owner, auto-login."""
        return self._request(
            "POST",
            "/v1/setup",
            auth=False,
            json_body={
                "workspace_name": workspace_name,
                "email": email,
                "password": password,
                "display_name": display_name,
            },
        )

    def login(
        self, *, email: str, password: str, device_label: str, device_id: str | None = None
    ) -> dict[str, Any]:
        """POST /v1/auth/login — verify credentials, mint/reuse device, open session."""
        body: dict[str, Any] = {
            "email": email,
            "password": password,
            "device_label": device_label,
        }
        if device_id is not None:
            body["device_id"] = device_id
        return self._request("POST", "/v1/auth/login", auth=False, json_body=body)

    def accept_invite(
        self, *, token: str, email: str, display_name: str, password: str
    ) -> dict[str, Any]:
        """POST /v1/auth/accept-invite — join a workspace via a single-use token."""
        return self._request(
            "POST",
            "/v1/auth/accept-invite",
            auth=False,
            json_body={
                "token": token,
                "email": email,
                "display_name": display_name,
                "password": password,
            },
        )

    def create_invite(
        self, *, role: str = "member", ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        """POST /v1/admin/invites — mint a single-use invite (owner/admin only)."""
        body: dict[str, Any] = {"role": role}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return self._request("POST", "/v1/admin/invites", auth=True, json_body=body)

    # --- sync / pull / push -------------------------------------------------

    def get_sync(self) -> dict[str, Any]:
        """GET /v1/sync — every readable stream + its ``head_seq``."""
        return self._request("GET", "/v1/sync", auth=True)

    def get_events(self, *, stream_id: str, after: int, limit: int) -> dict[str, Any]:
        """GET /v1/events — one forward page (``server_sequence > after``, ascending)."""
        return self._request(
            "GET",
            "/v1/events",
            auth=True,
            params={"stream_id": stream_id, "after": after, "limit": limit},
        )

    def post_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /v1/events/batch — sequence + idempotently store a batch (≤100 items).

        Transient faults retry the SAME batch inside :meth:`_request`; idempotency
        makes that safe. Returns ``{"accepted": [...], "rejected": [...]}``.
        """
        return self._request("POST", "/v1/events/batch", auth=True, json_body={"events": items})

    # --- functional helper --------------------------------------------------

    def with_token(self, token: str) -> None:
        """Adopt a freshly minted session token for subsequent authed calls."""
        self._token = token
