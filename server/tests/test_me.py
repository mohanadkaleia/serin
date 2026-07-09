"""Self-profile surface — ``GET /v1/me`` + ``PATCH /v1/me``.

The endpoint is STRUCTURALLY self-only (no ``user_id`` parameter anywhere), so
the teeth here are: the read returns the CALLER's row (not someone else's), the
PATCH updates exactly the caller's row + emits the ``user.profile_updated``
meta event the client directory folds renames from, validation mirrors the
signup ``DisplayName`` bounds, and there is no cross-user vector (a smuggled
``user_id`` in the body is ignored; a path-suffixed id is not a route).
"""

from __future__ import annotations

from typing import Any

from authutil import (
    accept_invite,
    auth_header,
    create_invite,
    do_setup,
    fetch_meta_stream_id,
    fetch_stream_events,
    join_token,
)
from httpx import AsyncClient
from msgd.db.models import User
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_member(client: AsyncClient, owner_token: str) -> dict[str, Any]:
    """Invite + accept a member; return their login body (token, user_id, ...)."""
    inv = await create_invite(client, owner_token, role="member")
    raw = join_token(inv.json()["url"])
    body: dict[str, Any] = (await accept_invite(client, raw, email="member@example.com")).json()
    return body


# --- read ---------------------------------------------------------------------


async def test_get_me_returns_the_callers_own_profile(client: AsyncClient) -> None:
    """Each caller sees THEIR row: id, display name, email, role, is_bot."""
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])

    resp = await client.get("/v1/me", headers=auth_header(owner["token"]))
    assert resp.status_code == 200
    assert resp.json() == {
        "user_id": owner["user_id"],
        "display_name": "The Owner",
        "email": "owner@example.com",
        "role": "owner",
        "is_bot": False,
    }

    resp = await client.get("/v1/me", headers=auth_header(member["token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == member["user_id"]
    assert body["display_name"] == "Invited User"
    assert body["email"] == "member@example.com"
    assert body["role"] == "member"


async def test_me_requires_authentication(client: AsyncClient) -> None:
    """No bearer → 401 on both the read and the write."""
    assert (await client.get("/v1/me")).status_code == 401
    assert (await client.patch("/v1/me", json={"display_name": "X"})).status_code == 401


# --- update -------------------------------------------------------------------


async def test_patch_me_updates_display_name_and_persists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PATCH returns the updated profile; the row and a re-read agree."""
    owner = await do_setup(client)
    h = auth_header(owner["token"])

    resp = await client.patch("/v1/me", json={"display_name": "Renamed Owner"}, headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Renamed Owner"
    assert body["user_id"] == owner["user_id"]
    # Non-name fields are untouched.
    assert body["email"] == "owner@example.com"
    assert body["role"] == "owner"

    # Persisted: the users row AND a fresh GET both show the new name.
    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.display_name == "Renamed Owner"
    assert (await client.get("/v1/me", headers=h)).json()["display_name"] == "Renamed Owner"


async def test_patch_me_emits_user_profile_updated_meta_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The rename appends exactly one self-authored ``user.profile_updated``.

    The client member directory is a fold over the workspace-meta log — this
    event is what renames the member on every client, so it must carry the new
    ``display_name`` and be authored by the caller (self, like ``user.joined``).
    """
    owner = await do_setup(client)
    meta_stream_id = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta_stream_id is not None
    before = await fetch_stream_events(db_session, meta_stream_id)

    resp = await client.patch(
        "/v1/me", json={"display_name": "New Name"}, headers=auth_header(owner["token"])
    )
    assert resp.status_code == 200

    after = await fetch_stream_events(db_session, meta_stream_id)
    assert len(after) == len(before) + 1
    event = after[-1]
    assert event.type == "user.profile_updated"
    body = event.body
    assert body["author_user_id"] == owner["user_id"]
    assert body["payload"]["user_id"] == owner["user_id"]
    assert body["payload"]["display_name"] == "New Name"


async def test_patch_me_rejects_empty_and_oversized_names(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The signup ``DisplayName`` bounds (1..200) apply: 422, row untouched."""
    owner = await do_setup(client)
    h = auth_header(owner["token"])

    for bad in ("", "x" * 201):
        resp = await client.patch("/v1/me", json={"display_name": bad}, headers=h)
        assert resp.status_code == 422, bad
        assert resp.json()["type"] == "/problems/validation-error"

    # A missing field is a 422 too (there is nothing else to PATCH).
    assert (await client.patch("/v1/me", json={}, headers=h)).status_code == 422

    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.display_name == "The Owner"


# --- structurally self-only ---------------------------------------------------


async def test_patch_me_has_no_cross_user_vector(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A member's PATCH changes ONLY the member; no route/body targets another user.

    ``/v1/me`` takes no ``user_id`` path parameter (a suffixed id is not a
    route), and a smuggled ``user_id`` in the body is ignored by the schema —
    the target is always the session's own user.
    """
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])
    h = auth_header(member["token"])

    # A smuggled user_id in the body is ignored: the member renames THEMSELVES.
    resp = await client.patch(
        "/v1/me", json={"display_name": "Sneaky", "user_id": owner["user_id"]}, headers=h
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == member["user_id"]

    member_row = await db_session.get(User, member["user_id"])
    owner_row = await db_session.get(User, owner["user_id"])
    assert member_row is not None and member_row.display_name == "Sneaky"
    assert owner_row is not None and owner_row.display_name == "The Owner"

    # There is no per-user route to aim at another account.
    resp = await client.patch(
        f"/v1/me/{owner['user_id']}", json={"display_name": "Nope"}, headers=h
    )
    assert resp.status_code in (404, 405)
