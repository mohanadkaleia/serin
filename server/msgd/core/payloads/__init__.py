"""Payload schema registry (TDD §2.2 / §2.3).

Maps ``(type, type_version)`` to the Pydantic model that validates a known
payload.  The envelope keeps ``payload`` an opaque dict; callers validate on
demand via :func:`get_payload_model`.  Unknown ``(type, version)`` pairs return
``None`` so the caller treats the payload as opaque (D9: skip in projection,
never crash).

Each event-type family gets its own module (``message.py``, later
``reaction.py``, ``membership.py``, ``file.py``) so future tickets add a module
and one registry line instead of conflicting on a single flat schemas file.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from msgd.core import ids
from msgd.core.envelope import Body
from msgd.core.payloads.message import MessageCreatedV1

__all__ = [
    "PAYLOAD_MODELS",
    "MessageCreatedV1",
    "get_payload_model",
    "build_message_created_body",
]

#: Registry of every known ``(type, type_version)`` payload model.
PAYLOAD_MODELS: dict[tuple[str, int], type[BaseModel]] = {
    ("message.created", 1): MessageCreatedV1,
}


def get_payload_model(type: str, type_version: int) -> type[BaseModel] | None:
    """Return the model for a known ``(type, type_version)``, else ``None``."""
    return PAYLOAD_MODELS.get((type, type_version))


def build_message_created_body(
    *,
    workspace_id: str,
    stream_id: str,
    author_user_id: str,
    author_device_id: str,
    client_created_at: str,
    text: str,
    format: str = "markdown",
    thread_root_id: str | None = None,
    file_ids: list[str] | None = None,
    mentions: list[str] | None = None,
    event_id: str | None = None,
    message_id: str | None = None,
) -> Body:
    """Mint and assemble a ``message.created`` v1 :class:`Body`.

    Mints ``event_id`` and ``message_id`` when not supplied, validates the
    payload through :class:`MessageCreatedV1`, and returns a :class:`Body` with
    the payload dumped to a plain dict.  Envelope finalization (attaching
    ``event_hash`` and ``server``) is left to ENG-56 / M1.
    """
    payload = MessageCreatedV1(
        message_id=message_id if message_id is not None else ids.new_message_id(),
        text=text,
        format=format,  # type: ignore[arg-type]  # validated by the model
        thread_root_id=thread_root_id,
        file_ids=file_ids if file_ids is not None else [],
        mentions=mentions if mentions is not None else [],
    )
    body_payload: dict[str, Any] = payload.model_dump(mode="json")
    return Body(
        event_id=event_id if event_id is not None else ids.new_event_id(),
        workspace_id=workspace_id,
        stream_id=stream_id,
        type="message.created",
        type_version=1,
        author_user_id=author_user_id,
        author_device_id=author_device_id,
        client_created_at=client_created_at,
        payload=body_payload,
    )
