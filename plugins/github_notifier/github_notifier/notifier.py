"""The delivery pipeline: verify -> dedupe -> format -> POST to the msg hook.

Transport-free core (the HTTP wrapper lives in :mod:`github_notifier.server`):
:meth:`Notifier.handle` takes the relevant headers + the raw body and returns
the status/JSON to answer GitHub with. Order is deliberate:

1. **Signature first**, over the raw bytes, before any parsing — an unsigned
   or tampered delivery is rejected without touching its content and without
   any outbound POST.
2. **Dedupe by ``X-GitHub-Delivery``** (GitHub redelivers at-least-once; the
   msg hook has no idempotency key, M5 Q6). A delivery id is recorded only
   AFTER a successful hook POST, so a failed forward stays retryable via
   GitHub's redelivery.
3. **Filter + format**: only ``pull_request`` events with a handled action
   produce a message; everything else is a 200 no-op (GitHub should not
   retry deliveries we chose to ignore).
4. **Forward** ``{"text": …}`` to the capability URL; the hook's
   ``200 {"ok": true}`` is the delivery receipt.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from github_notifier.config import Config
from github_notifier.dedupe import DeliveryLog
from github_notifier.formatting import format_pull_request
from github_notifier.signature import verify_signature

__all__ = ["Notifier", "Response", "post_to_hook"]

#: ``(hook_url, text) -> delivered?`` — the outbound seam (tests inject a fake).
PostFunc = Callable[[str, str], bool]


@dataclass(frozen=True)
class Response:
    """What to answer GitHub: an HTTP status + a small JSON body."""

    status: int
    body: dict[str, object]


def post_to_hook(
    hook_url: str,
    text: str,
    *,
    retries: int = 2,
    backoff_seconds: float = 0.5,
    timeout_seconds: float = 5.0,
) -> bool:
    """POST ``{"text": text}`` to the msg hook; ``True`` on its 200 ``{"ok":true}``.

    Simple bounded retry on transient failures (network errors, 5xx): up to
    ``retries`` extra attempts with exponential backoff. A 4xx is terminal —
    the capability URL is wrong/revoked (the hook's uniform 404) or the payload
    is unacceptable, and retrying cannot change that.
    """
    payload = json.dumps({"text": text}).encode("utf-8")
    for attempt in range(retries + 1):
        # The URL scheme was validated to http(s) at config load (no file:// etc.).
        request = urllib.request.Request(
            hook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return False  # terminal: revoked/unknown hook or rejected payload
        except (urllib.error.URLError, OSError, TimeoutError):
            pass  # transient: fall through to backoff + retry
        if attempt < retries:
            time.sleep(backoff_seconds * (2**attempt))
    return False


@dataclass
class Notifier:
    """The wired pipeline. ``post`` defaults to :func:`post_to_hook`."""

    config: Config
    deliveries: DeliveryLog = field(default_factory=DeliveryLog)
    post: PostFunc = post_to_hook

    def handle(
        self,
        *,
        event: str | None,
        delivery_id: str | None,
        signature: str | None,
        raw_body: bytes,
    ) -> Response:
        """Process one inbound GitHub delivery (see the module docstring for order)."""
        # 1. Signature over the RAW body, before anything else. Invalid/missing
        #    -> 401, no parse, no outbound POST (uniform: no missing-vs-wrong oracle).
        if not verify_signature(self.config.webhook_secret, raw_body, signature):
            return Response(401, {"error": "invalid signature"})

        # 2. Redelivery of something already forwarded -> ack without a second POST.
        if delivery_id is not None and delivery_id in self.deliveries:
            return Response(200, {"ok": True, "ignored": "duplicate delivery"})

        # 3. Only pull_request events (by header) are ours; the rest are acked no-ops.
        if event != "pull_request":
            return Response(200, {"ok": True, "ignored": f"event {event!r}"})
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response(400, {"error": "request body is not valid JSON"})
        if not isinstance(payload, dict):
            return Response(400, {"error": "request body must be a JSON object"})
        text = format_pull_request(payload)
        if text is None:
            return Response(200, {"ok": True, "ignored": "unhandled action"})

        # 4. Forward; record the delivery id only on success so a failed forward
        #    stays retryable through GitHub's redelivery (5xx makes GitHub retry).
        if not self.post(self.config.hook_url, text):
            return Response(502, {"error": "msg hook delivery failed"})
        if delivery_id is not None:
            self.deliveries.add(delivery_id)
        return Response(200, {"ok": True})
