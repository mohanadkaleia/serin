"""GitHub notifier — the msg reference plugin (ENG-162, TDD §10/D12).

An out-of-process bridge: it receives GitHub ``pull_request`` webhook
deliveries, verifies their HMAC signature, formats a one-line summary, and
POSTs ``{"text": …}`` to a msg incoming-webhook capability URL (the M5-2
``POST /v1/hooks/{hook_token}`` receiver).

This package exists to prove the public plugin API (docs/plugins.md) is real
and self-sufficient, so its ground rule is structural: it imports NOTHING from
``msgd``/``msgctl`` — stdlib only — and speaks to msg exclusively over HTTP.
``tests/test_notifier_imports.py`` enforces that rule.

Run it as ``python -m github_notifier`` with:

* ``GITHUB_WEBHOOK_SECRET`` — the shared secret configured on the GitHub
  webhook (used to verify ``X-Hub-Signature-256``). Required.
* ``MSG_HOOK_URL`` — the msg incoming-webhook capability URL to post to
  (minted once by ``POST /v1/plugins/hooks``). Required.
* ``GITHUB_NOTIFIER_HOST`` / ``GITHUB_NOTIFIER_PORT`` — bind address
  (default ``127.0.0.1:8477``; port ``0`` picks an ephemeral port).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
