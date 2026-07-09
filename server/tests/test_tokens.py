"""Tests for :mod:`msgd.auth.tokens` — the shared session/invite token minter.

ENG-148 regression: ``secrets.token_urlsafe`` may emit tokens starting with
``-`` or ``_``; a leading ``-`` makes argparse treat the token as an option
flag (``msgctl login --invite-token -20KX…``), which flaked the CLI e2e tests.
:func:`mint_token` must never return such a token. Over 5000 mints the old
implementation would emit at least one offender with probability
``1 - (62/64)^5000`` ≈ certainty, so this test is non-vacuous.
"""

from __future__ import annotations

import re

from msgd.auth.tokens import hash_token, mint_token

# token_urlsafe(32) → 43 chars of url-safe base64 (no padding).
_RAW_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def test_mint_token_never_starts_with_dash_or_underscore() -> None:
    """ENG-148: 5000 mints, none may start with ``-``/``_`` (argparse-safe)."""
    for _ in range(5000):
        raw, token_hash = mint_token()
        assert raw[0] not in "-_", f"minted token starts with {raw[0]!r}: {raw}"
        # Length + alphabet unchanged: still 43-char url-safe base64.
        assert _RAW_RE.fullmatch(raw), f"unexpected token shape: {raw!r}"
        assert token_hash == hash_token(raw)
