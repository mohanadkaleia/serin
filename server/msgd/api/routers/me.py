"""Self-profile router: ``GET /v1/me`` + ``PATCH /v1/me``.

STRUCTURALLY SELF-ONLY: neither endpoint takes a ``user_id`` — the target is
always ``ctx.user_id`` from the authenticated session, so there is no
cross-user vector to authorize away (contrast ``PATCH /v1/admin/members/{id}``,
which needs the whole policy matrix). Authz is simply "authenticated".

DISPLAY-NAME STORAGE (mirrors the codebase's split ownership):

* The ``users`` row is OPERATIONAL state authored by HTTP handlers
  (setup/accept-invite write it; ``reducers._reduce_noop`` documents that no
  reducer ever touches it) — so the PATCH updates the row directly.
* The rename is ALSO emitted as a server-authored ``user.profile_updated``
  meta event (§2.2): the client-side member directory is a fold over the
  workspace-meta log (``user.joined`` adds, ``user.profile_updated`` renames),
  so without the event no client — including the caller's own sidebar — would
  ever see the new name. Same handler-writes-row + handler-emits-meta-event
  shape as ``/v1/setup`` and ``/v1/auth/accept-invite``; ``user.profile_updated``
  stays non-client-writable (``can_write`` rejects it), exactly like
  ``user.joined``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.api.deps import CurrentAuth, require_scope
from msgd.api.schemas.me import MeResponse, UpdateMeRequest
from msgd.core.payloads import build_user_profile_updated_body
from msgd.core.time import now_rfc3339
from msgd.db.engine import get_session
from msgd.db.models import Stream, User
from msgd.events.emit import emit_event

router = APIRouter(prefix="/v1", tags=["me"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _me_response(user: User) -> MeResponse:
    """Project the caller's ``users`` row to the self-profile shape."""
    return MeResponse(
        user_id=user.user_id,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        is_bot=user.is_bot,
    )


@router.get("/me", response_model=MeResponse)
async def get_me(ctx: CurrentAuth) -> MeResponse:
    """The caller's own profile — the row ``require_auth`` already loaded."""
    return _me_response(ctx.user)


# ENG-159 (security review): PATCH /v1/me is a bot-reachable EVENT-LOG WRITE — it
# emits a server-authored ``user.profile_updated`` meta event — so it carries the
# same ``events:write`` verb gate as ``POST /v1/events/batch``. Without it a bot
# holding only ``events:read`` (or no write scope) could rename itself repeatedly
# and append unbounded meta events (the invariant-8 scope contract). Humans
# (``scopes is None``) bypass, so the self-rename golden path is unaffected. GET
# /v1/me stays ungated — it is a pure read of the caller's own identity.
@router.patch(
    "/me", response_model=MeResponse, dependencies=[Depends(require_scope("events:write"))]
)
async def update_me(req: UpdateMeRequest, ctx: CurrentAuth, db: DbSession) -> MeResponse:
    """Update the caller's own display name; return the updated profile.

    One transaction: lock + update the caller's ``users`` row (the row lock
    serializes concurrent self-PATCHes, mirroring the admin member PATCH), emit
    the server-authored ``user.profile_updated`` into the workspace-meta stream
    (authored by the caller on their current device, like their ``user.joined``),
    commit. Validation (non-empty, ≤200 chars) is the schema's ``DisplayName``
    — identical to the signup/accept constraint, enforced as a 422 upstream.
    """
    user = await db.scalar(select(User).where(User.user_id == ctx.user_id).with_for_update())
    assert user is not None  # require_auth just authenticated this user id
    user.display_name = req.display_name

    # Setup always creates the single workspace-meta stream (seq 1), so it must
    # exist for any real workspace — same assertion as accept-invite.
    meta_stream_id = await db.scalar(
        select(Stream.stream_id).where(
            Stream.workspace_id == ctx.workspace_id,
            Stream.kind == "workspace-meta",
        )
    )
    assert meta_stream_id is not None
    await emit_event(
        db,
        home_stream_id=meta_stream_id,
        body=build_user_profile_updated_body(
            workspace_id=ctx.workspace_id,
            stream_id=meta_stream_id,
            author_user_id=ctx.user_id,
            author_device_id=ctx.device_id,
            client_created_at=now_rfc3339(),
            user_id=ctx.user_id,
            display_name=req.display_name,
        ),
    )

    await db.commit()
    return _me_response(user)
