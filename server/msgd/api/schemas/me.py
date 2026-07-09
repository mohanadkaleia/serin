"""Self-profile request/response models (``/v1/me``).

``display_name`` reuses the exact :data:`~msgd.api.schemas.auth.DisplayName`
constraint enforced at signup/accept-invite (1..200 chars) — the profile edit
can never mint a name the join paths would have rejected.
"""

from __future__ import annotations

from pydantic import BaseModel

from msgd.api.schemas.auth import DisplayName


class MeResponse(BaseModel):
    """The caller's own profile (``GET /v1/me`` / the ``PATCH /v1/me`` echo).

    Mirrors the admin roster's ``MemberInfo`` field naming, minus the admin-only
    ``deactivated`` flag (a deactivated user cannot authenticate, so it would
    always be ``False`` here). ``email`` is the caller's OWN address — no other
    user's email ever leaves this self-scoped surface.
    """

    user_id: str
    display_name: str
    email: str
    role: str
    is_bot: bool


class UpdateMeRequest(BaseModel):
    """``PATCH /v1/me`` — the editable profile surface (display name only)."""

    display_name: DisplayName
