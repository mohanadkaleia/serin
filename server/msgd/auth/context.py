"""The ``AuthContext`` dependency contract (ENG-64 D5).

Every protected M1 router receives an :class:`AuthContext` from
``Depends(require_auth)``. It is a frozen read-snapshot of the authenticated
principal plus the loaded ORM rows (bound to the request session). Downstream
tickets (ENG-65+) read workspace scoping, membership, and author-field checks
off this object and must never re-parse the ``Authorization`` header.

ENG-159 (M5): a request may also be authenticated by a **bot token** (see
``msgd.auth.bot_tokens``). The bot branch yields the SAME context type — one
lookup path, one downstream contract — with two additive differences:

* ``scopes`` is a frozen set of verb scopes (``events:read`` / ``events:write``
  / ``files:write``). ``None`` means a HUMAN session — unscoped/full access.
  The ``require_scope`` dependency in ``msgd.api.deps`` is the only consumer.
* ``session`` is ``None`` (there is no ``sessions`` row for a bot token);
  ``session_token_hash`` carries the bot token's hash instead — still a sha256
  handle, never a credential.
"""

from __future__ import annotations

from dataclasses import dataclass

from msgd.db.models import Device, Session, User


@dataclass(frozen=True)
class AuthContext:
    """Immutable snapshot of the authenticated principal for a request."""

    user_id: str
    workspace_id: str
    role: str
    device_id: str
    session_token_hash: str
    # Live ORM rows (bound to the request's AsyncSession); routers that need
    # fresh fields reuse the same Depends(get_session) rather than re-querying.
    user: User
    device: Device
    # ``None`` for a bot-token principal (ENG-159): bot credentials live in
    # ``bot_tokens``, not ``sessions``.
    session: Session | None
    # Verb scopes carried by a BOT token; ``None`` = human session = unscoped.
    scopes: frozenset[str] | None = None
