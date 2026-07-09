"""``X-Hub-Signature-256`` verification — the plugin's one security-critical gate.

GitHub signs every delivery: the header value is ``sha256=`` + the hex
HMAC-SHA256 of the **raw request body** under the webhook's shared secret.
Anything that fails this check is dropped before the body is even parsed —
an unsigned or tampered delivery must never reach the msg hook.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["sign", "verify_signature"]

_PREFIX = "sha256="


def sign(secret: bytes, raw_body: bytes) -> str:
    """The expected ``X-Hub-Signature-256`` value for ``raw_body`` under ``secret``."""
    return _PREFIX + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, raw_body: bytes, header: str | None) -> bool:
    """Constant-time check of ``header`` against the RAW body's expected signature.

    The compare is :func:`hmac.compare_digest` over the full ``sha256=<hex>``
    strings (as bytes — ``compare_digest`` refuses non-ASCII ``str``), so a
    forger learns nothing from response timing. A missing header is simply
    ``False`` — the caller rejects without distinguishing missing vs wrong.
    """
    if header is None:
        return False
    expected = sign(secret, raw_body)
    return hmac.compare_digest(expected.encode("utf-8"), header.encode("utf-8"))
