"""Bot-token lifecycle (M5, ENG-159 D2-parity with :mod:`msgd.auth.sessions`).

Pure data-access helpers over an :class:`~sqlalchemy.ext.asyncio.AsyncSession`;
no FastAPI coupling (``msgd.api.deps.require_auth`` and the WS ``_authenticate``
compose these). Covers mint, lookup-by-hash, and the throttled ``last_used_at``
bump — the exact shape of ``create_session`` / ``lookup_session`` /
``bump_session``, minus expiry (a bot credential lives until revoked or its bot
is deactivated).

The token discipline is IDENTICAL to sessions/invites (D2): mint via
:func:`~msgd.auth.tokens.mint_token` (256-bit ``token_urlsafe``, no leading
``-``/``_`` — ENG-148), return the raw string exactly once, store only the
sha256 hex ``token_hash`` PK, look up by exact PK equality (no usable timing
surface on a full high-entropy hash).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.auth.sessions import utcnow
from msgd.auth.tokens import mint_token
from msgd.db.models import BotToken, Device, User
from msgd.settings import Settings

__all__ = [
    "KNOWN_SCOPES",
    "create_bot_token",
    "lookup_bot_token",
    "bump_bot_token",
]

#: The closed verb-scope vocabulary (§10): what a bot credential may DO.
#: WHERE it may do it stays the membership path (``stream_members`` grants via
#: ``channel.member_added`` + the guest readable-streams predicate) — scopes
#: never widen stream access, they only narrow verbs.
KNOWN_SCOPES = frozenset({"events:read", "events:write", "files:write"})


async def create_bot_token(
    db: AsyncSession,
    *,
    bot_user_id: str,
    workspace_id: str,
    scopes: list[str],
    created_by: str,
    now: datetime | None = None,
) -> tuple[BotToken, str]:
    """Mint a bot-token row and return ``(bot_token, raw_token)``.

    The raw token is returned to the caller once and never persisted; only its
    sha256 hex ``token_hash`` (PK) is stored (D2 — the ``create_session`` /
    ``create_invite`` discipline). ``scopes`` is stored as the JSONB list the
    caller validated against :data:`KNOWN_SCOPES` at the HTTP boundary.
    """
    raw, token_hash = mint_token()
    token = BotToken(
        token_hash=token_hash,
        bot_user_id=bot_user_id,
        workspace_id=workspace_id,
        scopes=sorted(set(scopes)),
        created_by=created_by,
        created_at=now or utcnow(),
    )
    db.add(token)
    return token, raw


async def lookup_bot_token(
    db: AsyncSession, token_hash: str
) -> tuple[BotToken, User, Device] | None:
    """Load ``(bot_token, bot_user, bot_device)`` by ``token_hash``, else ``None``.

    Exact PK equality (D2, like ``lookup_session``). The bot's device is the
    single device minted at provisioning; it is resolved deterministically
    (oldest first) so a hypothetical extra device could never make two requests
    disagree about the bot's ``author_device_id``. Returns ``None`` when the
    token, its user, or its device is missing — the caller maps every miss to
    the uniform 401 (revocation/deactivation checks are the CALLER's job so the
    401 stays uniform in one place).
    """
    row = (
        await db.execute(
            select(BotToken, User)
            .join(User, BotToken.bot_user_id == User.user_id)
            .where(BotToken.token_hash == token_hash)
        )
    ).first()
    if row is None:
        return None
    token, user = row[0], row[1]
    device = (
        await db.execute(
            select(Device)
            .where(Device.user_id == user.user_id)
            .order_by(Device.created_at, Device.device_id)
            .limit(1)
        )
    ).scalar()
    if device is None:
        return None
    return token, user, device


async def bump_bot_token(
    db: AsyncSession,
    token: BotToken,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    """Throttled ``last_used_at`` bump (the ``bump_session`` D4 pattern).

    Writes only when ``last_used_at`` is NULL (first use) or at least
    ``session_bump_interval_seconds`` old — at most one cheap PK UPDATE per
    interval per active token, never a write-per-request. Purely observability:
    ``last_used_at`` is never an authorization input and there is no rolling
    expiry to extend.
    """
    moment = now or utcnow()
    interval = timedelta(seconds=settings.session_bump_interval_seconds)
    if token.last_used_at is not None and moment - token.last_used_at < interval:
        return False
    token.last_used_at = moment
    return True
