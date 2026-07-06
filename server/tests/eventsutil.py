"""Typed helpers shared by the ENG-66 batch-upload tests.

Builds honest §3.2 wire items (``{body, event_hash}`` with the hash computed by
:func:`hash_event` over the verbatim body dict), posts batches, and bootstraps a
writable channel by uploading a ``channel.created`` genesis event through the
real endpoint.
"""

from __future__ import annotations

from typing import Any

from authutil import auth_header, fetch_meta_stream_id
from httpx import AsyncClient, Response
from msgd.core import ids
from msgd.core.hashing import hash_event
from msgd.core.payloads import build_message_created_body
from msgd.core.time import now_rfc3339
from sqlalchemy.ext.asyncio import AsyncSession

BATCH_URL = "/v1/events/batch"

#: The shape returned by ``do_setup`` / login responses that the builders read.
Auth = dict[str, Any]


def wire_item(body: dict[str, Any]) -> dict[str, Any]:
    """A §3.2 upload item with an HONEST hash over the verbatim ``body`` dict."""
    return {"body": body, "event_hash": hash_event(body)}


def message_body(
    *,
    auth: Auth,
    stream_id: str,
    text: str = "hello",
    **overrides: Any,
) -> dict[str, Any]:
    """A valid ``message.created`` v1 body authored by ``auth``'s principal.

    ``overrides`` are applied AFTER the model dump, onto the raw dict — so tests
    can produce deliberately nonconforming bodies (tampered scalars, bad ids)
    without the builder model rejecting them.
    """
    body = build_message_created_body(
        workspace_id=auth["workspace_id"],
        stream_id=stream_id,
        author_user_id=auth["user_id"],
        author_device_id=auth["device_id"],
        client_created_at=now_rfc3339(),
        text=text,
    ).model_dump(mode="json")
    body.update(overrides)
    return body


def channel_created_body(
    *,
    auth: Auth,
    home_stream_id: str,
    channel_stream_id: str | None = None,
    name: str = "general",
    visibility: str = "public",
) -> dict[str, Any]:
    """A ``channel.created`` v1 body (§2.2 homing is the CALLER's choice)."""
    return {
        "event_id": ids.new_event_id(),
        "workspace_id": auth["workspace_id"],
        "stream_id": home_stream_id,
        "type": "channel.created",
        "type_version": 1,
        "author_user_id": auth["user_id"],
        "author_device_id": auth["device_id"],
        "client_created_at": now_rfc3339(),
        "payload": {
            "channel_stream_id": (
                channel_stream_id if channel_stream_id is not None else ids.new_stream_id()
            ),
            "name": name,
            "visibility": visibility,
        },
    }


def lifecycle_body(
    *,
    auth: Auth,
    home_stream_id: str,
    type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """A channel lifecycle (renamed/archived/member_*) v1 body."""
    return {
        "event_id": ids.new_event_id(),
        "workspace_id": auth["workspace_id"],
        "stream_id": home_stream_id,
        "type": type,
        "type_version": 1,
        "author_user_id": auth["user_id"],
        "author_device_id": auth["device_id"],
        "client_created_at": now_rfc3339(),
        "payload": payload,
    }


def custom_body(
    *,
    auth: Auth,
    stream_id: str,
    type: str = "custom.thing",
    type_version: int = 1,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An unknown-type (D9) body with a valid envelope."""
    return {
        "event_id": ids.new_event_id(),
        "workspace_id": auth["workspace_id"],
        "stream_id": stream_id,
        "type": type,
        "type_version": type_version,
        "author_user_id": auth["user_id"],
        "author_device_id": auth["device_id"],
        "client_created_at": now_rfc3339(),
        "payload": payload if payload is not None else {"anything": True},
    }


async def post_batch(client: AsyncClient, token: str, items: list[Any]) -> Response:
    """POST the §3.2 batch request; the caller asserts on the response."""
    return await client.post(BATCH_URL, json={"events": items}, headers=auth_header(token))


async def bootstrap_channel(
    client: AsyncClient,
    db: AsyncSession,
    auth: Auth,
    *,
    visibility: str = "public",
    name: str = "general",
) -> str:
    """Create a channel through the real endpoint; return its stream id.

    §2.2 homing: a public genesis is homed in the workspace-meta stream, a
    private genesis is self-homed in the channel's own stream.
    """
    meta = await fetch_meta_stream_id(db, auth["workspace_id"])
    assert meta is not None
    channel_stream_id = ids.new_stream_id()
    home = meta if visibility == "public" else channel_stream_id
    body = channel_created_body(
        auth=auth,
        home_stream_id=home,
        channel_stream_id=channel_stream_id,
        name=name,
        visibility=visibility,
    )
    resp = await post_batch(client, auth["token"], [wire_item(body)])
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert len(payload["accepted"]) == 1, payload
    return channel_stream_id
