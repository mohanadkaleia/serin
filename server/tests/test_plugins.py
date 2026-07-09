"""Bot identity + bot tokens (ENG-159, M5-1) — adversarial acceptance tests.

Every test is a tooth: it fails if the guard it exercises is removed —

* the SERVER_AUTHORED forgery guard (a member's forged ``bot.installed`` would
  otherwise be ACCEPTED via the D9 ``can_read`` else-branch, the PR-#91 bug
  class);
* step-ii author binding in BOTH directions (bot↛human, human↛bot);
* the verb-scope gate (read-only vs write-only vs files:write, human bypass),
  on HTTP and on the WS connect;
* invariant-4 per-stream isolation for a granted-one-stream bot across write /
  pull / sync / search;
* instant revocation + deactivation bulk-revoke (+ exactly-once ``bot.removed``);
* owner/admin gating and the uniform 404s (unknown / cross-bot / wrong-kind
  ids all collapse).
"""

from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

import pytest
from authutil import (
    accept_invite,
    auth_header,
    create_invite,
    do_setup,
    fetch_meta_stream_id,
    fetch_stream_events,
    join_token,
)
from eventsutil import bootstrap_channel, message_body, post_batch, wire_item
from fastapi import FastAPI
from harness import make_ws_client
from httpx import AsyncClient, Response
from httpx_ws import WebSocketDisconnect, aconnect_ws
from httpx_ws._api import AsyncWebSocketSession
from msgd.core import ids
from msgd.db.models import BotToken, StreamMember, User
from msgd.export.restore import UNUSABLE_PASSWORD_HASH
from msgd.ws.frames import WSCloseCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

BOTS_URL = "/v1/plugins/bots"

_ALL_SCOPES = ["events:read", "events:write", "files:write"]

# --- helpers -------------------------------------------------------------------


async def _create_bot(
    client: AsyncClient,
    token: str,
    *,
    name: str = "Helper Bee",
    scopes: list[str] | None = None,
    stream_ids: list[str] | None = None,
) -> Response:
    """POST /v1/plugins/bots; the caller asserts on the response."""
    return await client.post(
        BOTS_URL,
        json={
            "name": name,
            "scopes": scopes if scopes is not None else _ALL_SCOPES,
            "stream_ids": stream_ids if stream_ids is not None else [],
        },
        headers=auth_header(token),
    )


async def _mint_token(
    client: AsyncClient,
    token: str,
    bot_user_id: str,
    *,
    scopes: list[str] | None = None,
) -> Response:
    """POST /v1/plugins/bots/{id}/tokens; ``scopes=None`` omits the field."""
    payload: dict[str, Any] = {} if scopes is None else {"scopes": scopes}
    return await client.post(
        f"{BOTS_URL}/{bot_user_id}/tokens", json=payload, headers=auth_header(token)
    )


async def _provision_bot(
    client: AsyncClient,
    owner: dict[str, Any],
    *,
    stream_ids: list[str],
    scopes: list[str] | None = None,
    token_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Create a bot + mint one token; return the eventsutil ``Auth`` dict shape.

    ``{token, user_id, device_id, workspace_id}`` — so ``message_body`` /
    ``post_batch`` drive the bot exactly like a human principal.
    """
    created = await _create_bot(client, owner["token"], scopes=scopes, stream_ids=stream_ids)
    assert created.status_code == 201, created.text
    bot = created.json()
    minted = await _mint_token(client, owner["token"], bot["bot_user_id"], scopes=token_scopes)
    assert minted.status_code == 201, minted.text
    return {
        "token": minted.json()["token"],
        "user_id": bot["bot_user_id"],
        "device_id": bot["device_id"],
        "workspace_id": owner["workspace_id"],
        "token_id": minted.json()["id"],
    }


async def _invite(client: AsyncClient, owner: dict[str, Any], *, role: str) -> dict[str, Any]:
    """Create + accept an invite; return the new user's auth dict."""
    invite = await create_invite(client, owner["token"], role=role)
    raw = join_token(invite.json()["url"])
    accepted = await accept_invite(client, raw, email=f"{ids.new_ulid().lower()}@example.com")
    assert accepted.status_code == 200, accepted.text
    body: dict[str, Any] = accepted.json()
    return body


def _meta_forgery_body(auth: dict[str, Any], meta: str, *, type_: str) -> dict[str, Any]:
    """An honestly-hashed ``bot.installed`` / ``bot.removed`` upload body."""
    payload: dict[str, Any] = {"bot_user_id": ids.new_user_id()}
    if type_ == "bot.installed":
        payload.update(name="Evil Bee", scopes=["events:write"])
    return {
        "event_id": ids.new_event_id(),
        "workspace_id": auth["workspace_id"],
        "stream_id": meta,
        "type": type_,
        "type_version": 1,
        "author_user_id": auth["user_id"],
        "author_device_id": auth["device_id"],
        "client_created_at": "2026-07-09T12:00:00.000Z",
        "payload": payload,
    }


async def _pull(client: AsyncClient, token: str, stream_id: str) -> Response:
    return await client.get(
        "/v1/events", params={"stream_id": stream_id}, headers=auth_header(token)
    )


def _meta_events_of_type(events: list[Any], type_: str) -> list[Any]:
    return [e for e in events if e.type == type_]


# --- bot creation: identity + meta events + grants -------------------------------


async def test_create_bot_identity_meta_events_and_grants(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """One create → bot users row (guest, sentinel hash), exactly one user.joined +
    one bot.installed, and event-sourced stream grants — and NO credential."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)

    resp = await _create_bot(
        client,
        owner["token"],
        scopes=["events:read", "events:write"],
        stream_ids=[channel, channel],  # duplicate id → single grant
    )
    assert resp.status_code == 201, resp.text
    bot = resp.json()
    assert "token" not in bot  # creation NEVER returns a credential
    bot_id = bot["bot_user_id"]
    assert bot["role"] == "guest"
    assert bot["device_id"].startswith("d_")
    assert bot["stream_ids"] == [channel]
    assert bot["tokens"] == []

    # The users row: is_bot, guest, the M4 unusable-password sentinel (no login).
    row = await db_session.get(User, bot_id)
    assert row is not None
    assert row.is_bot is True
    assert row.role == "guest"
    assert row.password_hash == UNUSABLE_PASSWORD_HASH
    assert row.deactivated_at is None

    # Meta log: exactly ONE user.joined and ONE bot.installed for this bot.
    meta = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta is not None
    events = await fetch_stream_events(db_session, meta)
    joined = [
        e
        for e in _meta_events_of_type(events, "user.joined")
        if e.body["payload"]["user_id"] == bot_id
    ]
    assert len(joined) == 1
    installed = _meta_events_of_type(events, "bot.installed")
    assert len(installed) == 1
    assert installed[0].body["payload"]["bot_user_id"] == bot_id
    assert installed[0].body["payload"]["scopes"] == ["events:read", "events:write"]
    assert installed[0].body["author_user_id"] == owner["user_id"]

    # The grant is EVENT-SOURCED: one channel.member_added for the bot (public
    # channel → homed in meta) whose reducer created the stream_members row.
    added = [
        e
        for e in _meta_events_of_type(events, "channel.member_added")
        if e.body["payload"]["user_id"] == bot_id
    ]
    assert len(added) == 1
    assert added[0].body["payload"]["channel_stream_id"] == channel
    member = await db_session.get(StreamMember, (channel, bot_id))
    assert member is not None

    # The admin roster shows the bot flagged is_bot.
    roster = await client.get("/v1/admin/members", headers=auth_header(owner["token"]))
    entry = next(m for m in roster.json()["members"] if m["user_id"] == bot_id)
    assert entry["is_bot"] is True
    assert entry["deactivated"] is False


async def test_bot_token_posts_message_to_granted_stream(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A bot token authenticates and authors a message.created into its granted
    stream; the stored author is the bot user."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    bot = await _provision_bot(client, owner, stream_ids=[channel])

    body = message_body(auth=bot, stream_id=channel, text="beep boop")
    resp = await post_batch(client, bot["token"], [wire_item(body)])
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["accepted"]) == 1, resp.text

    # The owner pulls the stream and sees the bot-authored message.
    page = await _pull(client, owner["token"], channel)
    authors = [e["body"]["author_user_id"] for e in page.json()["events"]]
    assert bot["user_id"] in authors


# --- step-ii author binding, both directions -------------------------------------


async def test_author_binding_bot_cannot_author_as_human(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    bot = await _provision_bot(client, owner, stream_ids=[channel])

    forged = message_body(auth=bot, stream_id=channel, author_user_id=owner["user_id"])
    resp = await post_batch(client, bot["token"], [wire_item(forged)])
    rejected = resp.json()["rejected"]
    assert len(rejected) == 1 and not resp.json()["accepted"]
    assert rejected[0]["code"] == "permission_denied"


async def test_author_binding_human_cannot_author_as_bot(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    bot = await _provision_bot(client, owner, stream_ids=[channel])

    forged = message_body(auth=bot, stream_id=channel)  # authored as the bot...
    resp = await post_batch(client, owner["token"], [wire_item(forged)])  # ...sent by a human
    rejected = resp.json()["rejected"]
    assert len(rejected) == 1 and not resp.json()["accepted"]
    assert rejected[0]["code"] == "permission_denied"


# --- forgery: the SERVER_AUTHORED guard -------------------------------------------


@pytest.mark.parametrize("type_", ["bot.installed", "bot.removed"])
async def test_member_cannot_forge_bot_meta_events(
    client: AsyncClient, db_session: AsyncSession, type_: str
) -> None:
    """A member's forged ``bot.installed``/``bot.removed`` upload is rejected by
    the SERVER_AUTHORED guard — non-vacuously.

    Non-vacuity: neither type is in ``_WRITE_MATRIX_TYPES``, so WITHOUT the
    guard the upload would fall to the D9 ``can_read`` else-branch, and a
    member CAN read workspace-meta — the event would be ACCEPTED. The asserted
    detail string is the guard's own (distinct from the ``can_write`` denial),
    proving exactly that line rejected it; zero stored events proves nothing
    slipped through another path.
    """
    owner = await do_setup(client)
    member = await _invite(client, owner, role="member")
    meta = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta is not None

    body = _meta_forgery_body(member, meta, type_=type_)
    resp = await post_batch(client, member["token"], [wire_item(body)])
    rejected = resp.json()["rejected"]
    assert len(rejected) == 1 and not resp.json()["accepted"]
    assert rejected[0]["code"] == "permission_denied"
    assert rejected[0]["detail"] == "event type is server-authored and cannot be uploaded"

    stored = _meta_events_of_type(await fetch_stream_events(db_session, meta), type_)
    assert stored == []


# --- the verb-scope matrix --------------------------------------------------------


async def test_scope_matrix(client: AsyncClient, db_session: AsyncSession) -> None:
    """events:read-only 403s writes; events:write-only 403s pull/sync; files:write
    gates initiate; a human session (scopes=None) bypasses everything."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)

    read_bot = await _provision_bot(
        client, owner, stream_ids=[channel], token_scopes=["events:read"]
    )
    write_bot = await _provision_bot(
        client, owner, stream_ids=[channel], token_scopes=["events:write"]
    )
    file_bot = await _provision_bot(
        client, owner, stream_ids=[channel], token_scopes=["files:write"]
    )

    # events:read-only → 403 on the write surface, 200 on pull + sync.
    body = message_body(auth=read_bot, stream_id=channel)
    denied = await post_batch(client, read_bot["token"], [wire_item(body)])
    assert denied.status_code == 403
    assert denied.json()["type"] == "/problems/forbidden"
    assert (await _pull(client, read_bot["token"], channel)).status_code == 200
    assert (await client.get("/v1/sync", headers=auth_header(read_bot["token"]))).status_code == 200

    # events:write-only → 200 on the write surface, 403 on pull + sync.
    body = message_body(auth=write_bot, stream_id=channel)
    accepted = await post_batch(client, write_bot["token"], [wire_item(body)])
    assert accepted.status_code == 200 and len(accepted.json()["accepted"]) == 1
    assert (await _pull(client, write_bot["token"], channel)).status_code == 403
    assert (
        await client.get("/v1/sync", headers=auth_header(write_bot["token"]))
    ).status_code == 403

    # files:write gates the upload surface both ways.
    initiate = {
        "sha256": "a" * 64,
        "name": "note.txt",
        "mime_type": "text/plain",
        "size_bytes": 4,
        "stream_id": channel,
    }
    no_files = await client.post(
        "/v1/files/initiate", json=initiate, headers=auth_header(write_bot["token"])
    )
    assert no_files.status_code == 403
    with_files = await client.post(
        "/v1/files/initiate", json=initiate, headers=auth_header(file_bot["token"])
    )
    assert with_files.status_code == 200, with_files.text

    # A human session is unscoped: every surface passes the gate.
    assert (await client.get("/v1/sync", headers=auth_header(owner["token"]))).status_code == 200
    assert (await _pull(client, owner["token"], channel)).status_code == 200
    human_body = message_body(auth=owner, stream_id=channel)
    human_post = await post_batch(client, owner["token"], [wire_item(human_body)])
    assert human_post.status_code == 200 and len(human_post.json()["accepted"]) == 1


# --- WS connect: scope + revocation ------------------------------------------------


def _aconnect(
    client: AsyncClient, token: str
) -> AbstractAsyncContextManager[AsyncWebSocketSession]:
    return cast(
        "AbstractAsyncContextManager[AsyncWebSocketSession]",
        aconnect_ws("http://test/v1/ws", client=client, subprotocols=["bearer", token]),
    )


async def _connect_expect_close(client: AsyncClient, token: str) -> int:
    """Connect and return the close code (pre-accept reject or accept-then-close)."""
    try:
        async with _aconnect(client, token) as ws:
            try:
                while True:
                    await ws.receive_json(timeout=2.0)
            except WebSocketDisconnect as exc:
                return exc.code
    except WebSocketDisconnect as exc:
        return exc.code
    raise AssertionError("expected the socket to be closed")  # pragma: no cover


async def test_ws_bot_scope_gate(ws_app: FastAPI, db_session: AsyncSession) -> None:
    """WS connect requires events:read on a bot token: read-scoped connects and
    receives granted-stream fanout; write-only closes 4403; revoked closes 4401."""
    async with make_ws_client(ws_app) as client:
        owner = await do_setup(client)
        channel = await bootstrap_channel(client, db_session, owner)
        read_bot = await _provision_bot(
            client, owner, stream_ids=[channel], token_scopes=["events:read"]
        )
        write_bot = await _provision_bot(
            client, owner, stream_ids=[channel], token_scopes=["events:write"]
        )

        # events:read → accepted; it receives fanout from its granted stream.
        async with _aconnect(client, read_bot["token"]) as ws:
            await ws.send_json({"t": "ping"})
            while (await ws.receive_json(timeout=2.0)).get("t") != "pong":
                pass  # drain until the registration barrier pong
            body = message_body(auth=owner, stream_id=channel, text="to the bot")
            posted = await post_batch(client, owner["token"], [wire_item(body)])
            assert len(posted.json()["accepted"]) == 1
            frame = await ws.receive_json(timeout=2.0)
            while frame.get("t") != "event":
                frame = await ws.receive_json(timeout=2.0)
            assert frame["event"]["body"]["stream_id"] == channel

        # events:write only → authenticated but unauthorized for the read surface.
        assert await _connect_expect_close(client, write_bot["token"]) == WSCloseCode.FORBIDDEN

        # A revoked token is the uniform 4401 (indistinguishable from unknown).
        revoke = await client.delete(
            f"{BOTS_URL}/{read_bot['user_id']}/tokens/{read_bot['token_id']}",
            headers=auth_header(owner["token"]),
        )
        assert revoke.status_code == 204
        assert await _connect_expect_close(client, read_bot["token"]) == WSCloseCode.UNAUTHENTICATED


# --- invariant 4: per-stream isolation ---------------------------------------------


async def test_bot_per_stream_isolation(client: AsyncClient, db_session: AsyncSession) -> None:
    """A bot granted only stream A cannot write B, cannot pull B (uniform 404),
    sees ONLY A in sync (not even workspace-meta — guests get no meta), and
    search returns zero hits from B."""
    owner = await do_setup(client)
    stream_a = await bootstrap_channel(client, db_session, owner, name="alpha")
    stream_b = await bootstrap_channel(
        client, db_session, owner, name="bravo", visibility="private"
    )

    # Seed content: a public hello in A, a secret in B.
    for stream, text in ((stream_a, "hello quokka"), (stream_b, "secret wombat")):
        posted = await post_batch(
            client,
            owner["token"],
            [wire_item(message_body(auth=owner, stream_id=stream, text=text))],
        )
        assert len(posted.json()["accepted"]) == 1

    bot = await _provision_bot(client, owner, stream_ids=[stream_a])

    # Write to B → the uniform permission_denied (identical to a nonexistent id).
    denied = await post_batch(
        client, bot["token"], [wire_item(message_body(auth=bot, stream_id=stream_b))]
    )
    assert denied.json()["rejected"][0]["code"] == "permission_denied"
    assert denied.json()["rejected"][0]["detail"] == "not permitted to write to this stream"

    # Pull B → uniform 404 (existence never disclosed); pull A works.
    assert (await _pull(client, bot["token"], stream_b)).status_code == 404
    assert (await _pull(client, bot["token"], stream_a)).status_code == 200

    # Sync: EXACTLY the granted stream — no B, and no workspace-meta (guest rule).
    sync = await client.get("/v1/sync", headers=auth_header(bot["token"]))
    assert [s["stream_id"] for s in sync.json()["streams"]] == [stream_a]

    # Search: the A hit is visible (non-vacuous), the B secret yields ZERO.
    hit = await client.get("/v1/search", params={"q": "quokka"}, headers=auth_header(bot["token"]))
    assert [r["stream_id"] for r in hit.json()["hits"]] == [stream_a]
    miss = await client.get("/v1/search", params={"q": "wombat"}, headers=auth_header(bot["token"]))
    assert miss.json()["hits"] == []


# --- stream grant/revoke endpoints --------------------------------------------------


async def test_stream_grant_and_revoke_endpoints(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PUT grants (member_added, §2.2-homed), DELETE revokes (member_removed) —
    and access follows immediately in both directions."""
    owner = await do_setup(client)
    public = await bootstrap_channel(client, db_session, owner, name="pub")
    private = await bootstrap_channel(client, db_session, owner, name="priv", visibility="private")
    bot = await _provision_bot(client, owner, stream_ids=[])

    # No grants yet: the bot cannot write the public channel (it is a guest).
    denied = await post_batch(
        client, bot["token"], [wire_item(message_body(auth=bot, stream_id=public))]
    )
    assert denied.json()["rejected"][0]["code"] == "permission_denied"

    for stream in (public, private):
        granted = await client.put(
            f"{BOTS_URL}/{bot['user_id']}/streams/{stream}", headers=auth_header(owner["token"])
        )
        assert granted.status_code == 204, granted.text
        accepted = await post_batch(
            client, bot["token"], [wire_item(message_body(auth=bot, stream_id=stream))]
        )
        assert len(accepted.json()["accepted"]) == 1, accepted.text

    # §2.2 homing: the PUBLIC grant event lives in workspace-meta, the PRIVATE
    # one is self-homed in the channel's own stream.
    meta = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta is not None
    meta_adds = [
        e
        for e in _meta_events_of_type(
            await fetch_stream_events(db_session, meta), "channel.member_added"
        )
        if e.body["payload"]["user_id"] == bot["user_id"]
    ]
    assert [e.body["payload"]["channel_stream_id"] for e in meta_adds] == [public]
    private_adds = [
        e
        for e in await fetch_stream_events(db_session, private)
        if e.type == "channel.member_added" and e.body["payload"]["user_id"] == bot["user_id"]
    ]
    assert len(private_adds) == 1

    # Revoke the private grant: membership row gone, write + pull cut immediately.
    revoked = await client.delete(
        f"{BOTS_URL}/{bot['user_id']}/streams/{private}", headers=auth_header(owner["token"])
    )
    assert revoked.status_code == 204
    assert await db_session.get(StreamMember, (private, bot["user_id"])) is None
    after = await post_batch(
        client, bot["token"], [wire_item(message_body(auth=bot, stream_id=private))]
    )
    assert after.json()["rejected"][0]["code"] == "permission_denied"
    assert (await _pull(client, bot["token"], private)).status_code == 404


# --- revocation + deactivation -------------------------------------------------------


async def test_token_revocation_is_instant(client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    bot = await _provision_bot(client, owner, stream_ids=[channel])
    second = await _mint_token(client, owner["token"], bot["user_id"])
    assert second.status_code == 201

    revoke = await client.delete(
        f"{BOTS_URL}/{bot['user_id']}/tokens/{bot['token_id']}",
        headers=auth_header(owner["token"]),
    )
    assert revoke.status_code == 204

    # The revoked bearer 401s on its very next request; the sibling still works.
    dead = await client.get("/v1/sync", headers=auth_header(bot["token"]))
    assert dead.status_code == 401
    alive = await client.get("/v1/sync", headers=auth_header(second.json()["token"]))
    assert alive.status_code == 200

    # Re-revoking the same handle is the uniform 404 (no revoked-vs-unknown oracle).
    again = await client.delete(
        f"{BOTS_URL}/{bot['user_id']}/tokens/{bot['token_id']}",
        headers=auth_header(owner["token"]),
    )
    assert again.status_code == 404


async def test_deactivation_bulk_revokes_and_emits_bot_removed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Deactivating a bot kills EVERY outstanding token in the same transaction
    and emits exactly one bot.removed; re-deactivation is an idempotent no-op."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    bot = await _provision_bot(client, owner, stream_ids=[channel])
    second = (await _mint_token(client, owner["token"], bot["user_id"])).json()

    patched = await client.patch(
        f"/v1/admin/members/{bot['user_id']}",
        json={"active": False},
        headers=auth_header(owner["token"]),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["deactivated"] is True

    # Both bearers die NOW (rows hard-deleted), uniform 401.
    for raw in (bot["token"], second["token"]):
        resp = await client.get("/v1/sync", headers=auth_header(raw))
        assert resp.status_code == 401
    remaining = (
        (await db_session.execute(select(BotToken).where(BotToken.bot_user_id == bot["user_id"])))
        .scalars()
        .all()
    )
    assert remaining == []

    # Exactly one bot.removed in the meta log, authored by the acting admin.
    meta = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta is not None
    removed = _meta_events_of_type(await fetch_stream_events(db_session, meta), "bot.removed")
    assert len(removed) == 1
    assert removed[0].body["payload"]["bot_user_id"] == bot["user_id"]
    assert removed[0].body["author_user_id"] == owner["user_id"]

    # Idempotent re-deactivation: 200, still exactly one bot.removed.
    patched = await client.patch(
        f"/v1/admin/members/{bot['user_id']}",
        json={"active": False},
        headers=auth_header(owner["token"]),
    )
    assert patched.status_code == 200
    removed = _meta_events_of_type(await fetch_stream_events(db_session, meta), "bot.removed")
    assert len(removed) == 1

    # No fresh credentials for a deactivated bot.
    minted = await _mint_token(client, owner["token"], bot["user_id"])
    assert minted.status_code == 403


# --- gating + uniform 404s ------------------------------------------------------------


async def test_plugins_surface_is_owner_admin_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """member/guest — and the bot itself (a guest) — are 403d by require_role."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    member = await _invite(client, owner, role="member")
    guest = await _invite(client, owner, role="guest")
    admin = await _invite(client, owner, role="admin")
    bot = await _provision_bot(client, owner, stream_ids=[channel])

    for caller in (member["token"], guest["token"], bot["token"]):
        assert (await _create_bot(client, caller)).status_code == 403
        assert (await client.get(BOTS_URL, headers=auth_header(caller))).status_code == 403
        assert (await _mint_token(client, caller, bot["user_id"])).status_code == 403
        assert (
            await client.delete(
                f"{BOTS_URL}/{bot['user_id']}/tokens/{bot['token_id']}",
                headers=auth_header(caller),
            )
        ).status_code == 403
        assert (
            await client.put(
                f"{BOTS_URL}/{bot['user_id']}/streams/{channel}", headers=auth_header(caller)
            )
        ).status_code == 403

    # An admin (not just the owner) may drive the surface.
    assert (await client.get(BOTS_URL, headers=auth_header(admin["token"]))).status_code == 200


async def test_uniform_404s(client: AsyncClient, db_session: AsyncSession) -> None:
    """Unknown ids, human ids, wrong-bot token handles, and non-channel streams
    all collapse to the identical not_found."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    bot_a = await _provision_bot(client, owner, stream_ids=[channel])
    bot_b = await _provision_bot(client, owner, stream_ids=[channel])
    meta = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta is not None
    headers = auth_header(owner["token"])

    bodies: list[dict[str, Any]] = []

    # Unknown bot id / a HUMAN user id as bot id → 404 on mint.
    for target in (ids.new_user_id(), owner["user_id"]):
        resp = await _mint_token(client, owner["token"], target)
        assert resp.status_code == 404
        bodies.append(resp.json())

    # Token revoke: unknown handle, and bot A's real handle under bot B's path.
    for bot_id, handle in (
        (bot_a["user_id"], "0" * 64),
        (bot_b["user_id"], bot_a["token_id"]),
    ):
        resp = await client.delete(f"{BOTS_URL}/{bot_id}/tokens/{handle}", headers=headers)
        assert resp.status_code == 404
        bodies.append(resp.json())
    # ...and the cross-bot attempt did NOT revoke A's token.
    assert (await client.get("/v1/sync", headers=auth_header(bot_a["token"]))).status_code == 200

    # Stream grant: unknown stream, and a NON-channel stream (workspace-meta).
    for stream in (ids.new_stream_id(), meta):
        resp = await client.put(f"{BOTS_URL}/{bot_a['user_id']}/streams/{stream}", headers=headers)
        assert resp.status_code == 404
        bodies.append(resp.json())

    # Uniformity: every miss is the same problem type + status (no oracle by shape).
    assert {b["type"] for b in bodies} == {"/problems/not-found"}
    assert {b["status"] for b in bodies} == {404}


# --- token discipline -------------------------------------------------------------------


async def test_token_raw_once_hash_stored_and_default_scopes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The raw token appears exactly once (mint); storage and every listing carry
    only its sha256; omitted mint scopes default to the INSTALL scopes."""
    owner = await do_setup(client)
    channel = await bootstrap_channel(client, db_session, owner)
    created = await _create_bot(
        client, owner["token"], scopes=["events:write"], stream_ids=[channel]
    )
    bot_id = created.json()["bot_user_id"]

    minted = await _mint_token(client, owner["token"], bot_id)  # scopes omitted
    assert minted.status_code == 201
    raw = minted.json()["token"]
    handle = minted.json()["id"]

    # Hash discipline: stored handle == sha256(raw); the raw is NOT stored.
    assert handle == hashlib.sha256(raw.encode()).hexdigest()
    row = await db_session.get(BotToken, handle)
    assert row is not None
    assert raw not in (row.token_hash, row.bot_user_id, row.workspace_id)
    # ENG-148 discipline: never a leading '-'/'_' (argv-safe).
    assert raw[0] not in "-_"

    # Default scopes == the install scopes recorded in bot.installed.
    assert minted.json()["scopes"] == ["events:write"]
    assert row.scopes == ["events:write"]
    # An explicit mint overrides (still within the closed vocabulary)...
    explicit = await _mint_token(client, owner["token"], bot_id, scopes=["events:read"])
    assert explicit.json()["scopes"] == ["events:read"]
    # ...and an unknown scope string is a 422 at the boundary, never minted.
    bad = await _mint_token(client, owner["token"], bot_id, scopes=["events:admin"])
    assert bad.status_code == 422

    # The listing exposes hash handles only — the raw token appears nowhere.
    listing = await client.get(BOTS_URL, headers=auth_header(owner["token"]))
    assert raw not in listing.text
    entry = next(b for b in listing.json()["bots"] if b["bot_user_id"] == bot_id)
    assert {t["id"] for t in entry["tokens"]} >= {handle}
