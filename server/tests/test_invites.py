"""Invites — create (owner/admin), single-use, TTL, role restriction (D7)."""

from __future__ import annotations

from datetime import timedelta

from authutil import (
    accept_invite,
    auth_header,
    create_invite,
    do_setup,
    join_token,
)
from httpx import AsyncClient
from msgd.auth.sessions import utcnow
from msgd.auth.tokens import hash_token
from msgd.db.models import Invite
from sqlalchemy.ext.asyncio import AsyncSession


async def test_owner_creates_invite(client: AsyncClient) -> None:
    """An owner mints an invite and gets a one-time join URL back."""
    owner_token = (await do_setup(client))["token"]
    resp = await create_invite(client, owner_token, role="member")
    assert resp.status_code == 201
    body = resp.json()
    assert "/join/" in body["url"]
    assert body["expires_at"]


async def test_accept_creates_user_and_autologin(client: AsyncClient) -> None:
    """Accepting an invite creates the user with the invite role and logs in."""
    owner_token = (await do_setup(client))["token"]
    invite = await create_invite(client, owner_token, role="member")
    raw = join_token(invite.json()["url"])

    accepted = await accept_invite(client, raw, email="joiner@example.com")
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["role"] == "member"
    assert body["token"]
    # The minted token authenticates.
    me = await client.get("/v1/auth/sessions", headers=auth_header(body["token"]))
    assert me.status_code == 200


async def test_member_cannot_create_invite(client: AsyncClient) -> None:
    """A member hitting the admin invite endpoint is forbidden (403)."""
    owner_token = (await do_setup(client))["token"]
    invite = await create_invite(client, owner_token, role="member")
    raw = join_token(invite.json()["url"])
    member_token = (await accept_invite(client, raw, email="m@example.com")).json()["token"]

    resp = await create_invite(client, member_token, role="member")
    assert resp.status_code == 403
    assert resp.json()["type"] == "/problems/forbidden"


async def test_single_use_second_accept_410(client: AsyncClient) -> None:
    """A second accept of the same token is rejected (single-use, 410)."""
    owner_token = (await do_setup(client))["token"]
    invite = await create_invite(client, owner_token, role="member")
    raw = join_token(invite.json()["url"])

    first = await accept_invite(client, raw, email="first@example.com")
    assert first.status_code == 200
    second = await accept_invite(client, raw, email="second@example.com")
    assert second.status_code == 410
    assert second.json()["type"] == "/problems/invite-used"


async def test_expired_invite_410(client: AsyncClient, db_session: AsyncSession) -> None:
    """An expired invite is rejected 410 invite-expired."""
    owner_token = (await do_setup(client))["token"]
    invite = await create_invite(client, owner_token, role="member")
    raw = join_token(invite.json()["url"])

    row = await db_session.get(Invite, hash_token(raw))
    assert row is not None
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.flush()

    resp = await accept_invite(client, raw, email="late@example.com")
    assert resp.status_code == 410
    assert resp.json()["type"] == "/problems/invite-expired"


async def test_invite_cannot_request_owner_role(client: AsyncClient) -> None:
    """An invite may not mint an owner — the schema rejects it as 422."""
    owner_token = (await do_setup(client))["token"]
    resp = await client.post(
        "/v1/admin/invites", json={"role": "owner"}, headers=auth_header(owner_token)
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"


async def test_admin_role_can_create_invite(client: AsyncClient) -> None:
    """An 'admin' (invited as admin) can also create invites (owner/admin gate)."""
    owner_token = (await do_setup(client))["token"]
    admin_invite = await create_invite(client, owner_token, role="admin")
    raw = join_token(admin_invite.json()["url"])
    admin_token = (await accept_invite(client, raw, email="admin@example.com")).json()["token"]

    resp = await create_invite(client, admin_token, role="member")
    assert resp.status_code == 201
