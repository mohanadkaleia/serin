"""Auth router (ENG-64): setup, login, sessions list/revoke, accept-invite.

Every endpoint here errors via problem+json (registered app-wide). The three
credential-taking POSTs (setup, login, accept-invite) are gated by the auth
rate limiter (per-IP + per-email, D6) *before* any argon2 work runs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.api import problems
from msgd.api.deps import AppSettings, CurrentAuth, auth_rate_limit
from msgd.api.schemas.auth import (
    AcceptInviteRequest,
    LoginRequest,
    LoginResponse,
    SessionInfo,
    SessionListResponse,
    SetupRequest,
)
from msgd.auth.passwords import (
    dummy_verify,
    hash_password_async,
    needs_rehash,
    verify_password,
)
from msgd.auth.sessions import (
    create_session,
    list_sessions,
    mint_or_reuse_device,
    revoke_session,
    utcnow,
)
from msgd.auth.tokens import hash_token
from msgd.core.ids import new_user_id, new_workspace_id
from msgd.db.engine import get_session
from msgd.db.models import Device, Invite, User, Workspace

router = APIRouter(prefix="/v1", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_session)]

# Fixed advisory-lock key for first-run setup (D1). hashtext() maps the label to
# an int; pg_advisory_xact_lock serializes concurrent setups within a txn.
_SETUP_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext('msg:setup'))")


async def _login_response(
    db: AsyncSession,
    *,
    user: User,
    device: Device,
    settings: AppSettings,
) -> LoginResponse:
    """Mint a session for ``user``/``device`` and shape the login response."""
    session, raw = await create_session(
        db, user_id=user.user_id, device_id=device.device_id, settings=settings
    )
    await db.commit()
    return LoginResponse(
        token=raw,
        user_id=user.user_id,
        device_id=device.device_id,
        workspace_id=user.workspace_id,
        role=user.role,
        expires_at=session.expires_at,
    )


@router.post("/setup", response_model=LoginResponse, dependencies=[Depends(auth_rate_limit)])
async def setup(req: SetupRequest, db: DbSession, settings: AppSettings) -> LoginResponse:
    """First-run: create the workspace + owner, auto-login (D1).

    Valid only while zero users exist. A transaction-scoped Postgres advisory
    lock serializes concurrent setups; the loser sees ``count > 0`` and gets 409.
    """
    # Advisory lock FIRST — serialize any concurrent setup attempts (D1).
    await db.execute(_SETUP_LOCK_SQL)
    user_count = await db.scalar(select(func.count()).select_from(User))
    if user_count:
        raise problems.already_initialized()

    workspace_id = new_workspace_id()
    user_id = new_user_id()
    password_hash = await hash_password_async(settings, req.password)
    # No ORM relationships are declared (models.py), so the unit-of-work does not
    # auto-order these FK-linked inserts — flush the workspace before the user.
    db.add(Workspace(workspace_id=workspace_id, name=req.workspace_name))
    await db.flush()
    db.add(
        User(
            user_id=user_id,
            workspace_id=workspace_id,
            email=req.email,
            password_hash=password_hash,
            display_name=req.display_name,
            role="owner",
        )
    )
    await db.flush()

    # ENG-65 seam: emit workspace.created + user.joined to workspace-meta here.
    # Deferred to the streams ticket — no fake stream is stubbed (plan D1).

    device = await mint_or_reuse_device(db, user_id=user_id, device_label=None, device_id=None)
    assert device is not None  # mint path (no device_id) never returns None
    await db.flush()
    user = await db.get(User, user_id)
    assert user is not None
    return await _login_response(db, user=user, device=device, settings=settings)


@router.post("/auth/login", response_model=LoginResponse, dependencies=[Depends(auth_rate_limit)])
async def login(req: LoginRequest, db: DbSession, settings: AppSettings) -> LoginResponse:
    """Verify credentials (argon2id), mint/reuse a device, and open a session."""
    # Single-workspace MVP: email is effectively global (§7/D6).
    user = await db.scalar(select(User).where(User.email == req.email).limit(1))
    if user is None or user.deactivated_at is not None:
        # Unknown email / deactivated: burn an identical argon2 verify against a
        # dummy hash, then return the *same* generic 401 as a wrong password —
        # no enumeration by timing or by response shape (D2).
        await dummy_verify(settings, req.password)
        raise problems.invalid_credentials()

    if not await verify_password(settings, user.password_hash, req.password):
        raise problems.invalid_credentials()

    # Wired for a future param-upgrade flow; M1 checks but does not act (D8).
    needs_rehash(settings, user.password_hash)

    device = await mint_or_reuse_device(
        db,
        user_id=user.user_id,
        device_label=req.device_label,
        device_id=req.device_id,
    )
    if device is None:
        # Runs only after successful auth → discloses nothing about credentials.
        raise problems.invalid_device()
    await db.flush()
    return await _login_response(db, user=user, device=device, settings=settings)


@router.get("/auth/sessions", response_model=SessionListResponse)
async def list_user_sessions(ctx: CurrentAuth, db: DbSession) -> SessionListResponse:
    """List the caller's sessions; the current session is flagged (D5).

    ``id`` is the session ``token_hash`` — not a credential (it cannot be
    reversed to the bearer token) and a user only ever sees their own (§Risks).
    """
    sessions = await list_sessions(db, ctx.user_id)
    device_ids = {s.device_id for s in sessions}
    labels: dict[str, str | None] = {}
    if device_ids:
        rows = await db.execute(select(Device).where(Device.device_id.in_(device_ids)))
        labels = {d.device_id: d.label for d in rows.scalars()}
    return SessionListResponse(
        sessions=[
            SessionInfo(
                id=s.token_hash,
                device_id=s.device_id,
                device_label=labels.get(s.device_id),
                created_at=s.created_at,
                last_seen_at=s.last_seen_at,
                expires_at=s.expires_at,
                current=s.token_hash == ctx.session_token_hash,
            )
            for s in sessions
        ]
    )


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke_user_session(session_id: str, ctx: CurrentAuth, db: DbSession) -> None:
    """Revoke one of the caller's sessions (instant — next request 401s).

    Scoped to the owner: revoking another user's session id matches no row → 404.
    """
    deleted = await revoke_session(db, token_hash=session_id, user_id=ctx.user_id)
    if not deleted:
        raise problems.not_found("no such session")
    await db.commit()


@router.post(
    "/auth/accept-invite",
    response_model=LoginResponse,
    dependencies=[Depends(auth_rate_limit)],
)
async def accept_invite(
    req: AcceptInviteRequest, db: DbSession, settings: AppSettings
) -> LoginResponse:
    """Accept an invite (unauthenticated — the token is the authorization, D7).

    Single-use is enforced by an atomic ``UPDATE ... WHERE used_by IS NULL
    RETURNING``: two concurrent accepts race on that update and exactly one wins.
    """
    now = utcnow()
    token_hash = hash_token(req.token)
    invite = await db.get(Invite, token_hash)
    if invite is None:
        raise problems.invalid_invite()
    if invite.used_by is not None:
        raise problems.invite_used()
    if now >= invite.expires_at:
        raise problems.invite_expired()

    # Guard email uniqueness before consuming the invite so a duplicate email
    # doesn't burn a single-use invite (UNIQUE(workspace_id, email)).
    existing = await db.scalar(
        select(User)
        .where(User.workspace_id == invite.workspace_id, User.email == req.email)
        .limit(1)
    )
    if existing is not None:
        raise problems.forbidden("an account with this email already exists")

    new_user_ident = new_user_id()
    claimed = await db.execute(
        update(Invite)
        .where(Invite.token_hash == token_hash, Invite.used_by.is_(None))
        .values(used_by=new_user_ident)
        .returning(Invite.token_hash)
    )
    if claimed.first() is None:
        # Lost the single-use race — another accept consumed it first.
        raise problems.invite_used()

    password_hash = await hash_password_async(settings, req.password)
    db.add(
        User(
            user_id=new_user_ident,
            workspace_id=invite.workspace_id,
            email=req.email,
            password_hash=password_hash,
            display_name=req.display_name,
            role=invite.role,
        )
    )
    await db.flush()

    # ENG-65 seam: emit user.joined to workspace-meta here (deferred, plan D7).

    device = await mint_or_reuse_device(
        db, user_id=new_user_ident, device_label=None, device_id=None
    )
    assert device is not None
    await db.flush()
    user = await db.get(User, new_user_ident)
    assert user is not None
    return await _login_response(db, user=user, device=device, settings=settings)
