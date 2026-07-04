"""Event envelope models — the wire and storage shape of an event (TDD §2.1).

An event has two sections plus two top-level fields::

    { "body": {...}, "event_hash": "...", "signature": null, "server": {...} }

``event_hash`` = SHA-256 over the RFC 8785 (JCS) canonicalization of ``body``
*only*.  The server never mutates ``body``; everything it knows goes in
``server``.  ENG-54 treats ``event_hash`` as an opaque string — computing and
verifying it belongs to ENG-56.

Three modeling calls are **locked here** and must not be "cleaned up" later:

1.  ``extra="allow"`` on every model on the envelope path (:class:`Body`,
    :class:`ServerMetadata`, :class:`Envelope`).  This is what makes *unknown
    fields survive a round trip* (§2.3): they are retained as attributes and
    re-emitted by ``model_dump``.  Pydantic's default ``extra="ignore"`` would
    silently drop them and break the byte-lossless acceptance criterion.

2.  ``payload`` is an opaque ``dict[str, Any]`` on :class:`Body`, never a typed
    union.  A discriminated union would *raise* on an unknown ``type`` —
    violating D9's "unknown types are preserved, never crash".  Known payloads
    are validated on demand via :mod:`msgd.core.payloads`, not by coercing them
    into the envelope.

3.  Timestamps are validated ``str``, never ``datetime``.  The acceptance
    criterion is byte-lossless re-serialization of the §2.1 example, and
    Pydantic's ``datetime`` round-trip mutates the text (``.123Z`` →
    ``.123000+00:00``).  ENG-56 hashes JCS(body), so the body must serialize
    back to exactly what the client sent.  D14 makes ``client_created_at``
    untrusted metadata anyway — there is no reason to parse it here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator

from msgd.core import ids

__all__ = [
    "MAX_EVENT_SIZE_BYTES",
    "EventTooLargeError",
    "Body",
    "ServerMetadata",
    "Envelope",
    "serialized_size_bytes",
    "check_event_size",
]

#: Hard limit on the serialized size of a single event (TDD §2.1 / §4.3),
#: measured over the §3.2 upload wire form ``{body, event_hash}``.
MAX_EVENT_SIZE_BYTES: Final = 64 * 1024

# RFC 3339 / ISO-8601 timestamp with a mandatory ``Z`` or numeric offset.
# We validate the *shape* but preserve the original text verbatim (see the
# module docstring, locked call 3).
# Shape-only by design: structure is validated, not value ranges
# (``2026-13-45T99:99:99Z`` passes). Range checks add no value here —
# ``client_created_at`` is untrusted metadata (D14) and preserved verbatim,
# ``server_received_at`` is server-minted, and server time is authoritative.
_RFC3339_RE: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


class EventTooLargeError(ValueError):
    """Raised when a serialized event exceeds :data:`MAX_EVENT_SIZE_BYTES`."""

    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__(
            f"serialized event is {size} bytes, exceeds the {MAX_EVENT_SIZE_BYTES}-byte limit"
        )


def _validate_rfc3339(value: str) -> str:
    """Validate an RFC 3339-ish timestamp string, returning it unchanged."""
    if not _RFC3339_RE.match(value):
        raise ValueError(f"not an RFC 3339 timestamp: {value!r}")
    return value


class ServerMetadata(BaseModel):
    """Unhashed server-assigned metadata (absent on the client upload form)."""

    model_config = ConfigDict(extra="allow")

    server_sequence: int
    server_received_at: str
    #: Reserved for post-MVP redaction (§2.1); ships now, defaults false.
    payload_redacted: bool = False

    @field_validator("server_received_at")
    @classmethod
    def _check_server_received_at(cls, value: str) -> str:
        return _validate_rfc3339(value)


class Body(BaseModel):
    """The hashed client body.  ``event_hash`` is SHA-256 of JCS(this)."""

    model_config = ConfigDict(extra="allow")

    event_id: str
    workspace_id: str
    stream_id: str
    type: str
    type_version: int
    author_user_id: str
    author_device_id: str
    client_created_at: str
    #: Opaque per §2.3 — kept a dict so unknown event types round-trip losslessly.
    payload: dict[str, Any]

    @field_validator("event_id")
    @classmethod
    def _check_event_id(cls, value: str) -> str:
        if not ids.is_valid_ulid(value):
            raise ValueError(f"event_id is not a valid ULID: {value!r}")
        return value

    @field_validator("workspace_id")
    @classmethod
    def _check_workspace_id(cls, value: str) -> str:
        if not ids.is_valid_typed_id(value, ids.IdKind.WORKSPACE):
            raise ValueError(f"workspace_id is not a valid w_ id: {value!r}")
        return value

    @field_validator("stream_id")
    @classmethod
    def _check_stream_id(cls, value: str) -> str:
        if not ids.is_valid_typed_id(value, ids.IdKind.STREAM):
            raise ValueError(f"stream_id is not a valid s_ id: {value!r}")
        return value

    @field_validator("author_user_id")
    @classmethod
    def _check_author_user_id(cls, value: str) -> str:
        if not ids.is_valid_typed_id(value, ids.IdKind.USER):
            raise ValueError(f"author_user_id is not a valid u_ id: {value!r}")
        return value

    @field_validator("author_device_id")
    @classmethod
    def _check_author_device_id(cls, value: str) -> str:
        if not ids.is_valid_typed_id(value, ids.IdKind.DEVICE):
            raise ValueError(f"author_device_id is not a valid d_ id: {value!r}")
        return value

    @field_validator("client_created_at")
    @classmethod
    def _check_client_created_at(cls, value: str) -> str:
        return _validate_rfc3339(value)


class Envelope(BaseModel):
    """A full event: hashed ``body`` + opaque hash + reserved sig + server meta."""

    model_config = ConfigDict(extra="allow")

    body: Body
    #: Opaque here (``"sha256:..."``); computed/verified by ENG-56.
    event_hash: str
    #: Reserved for federation-era device signatures; always null in MVP (D1).
    signature: str | None = None
    #: Absent on the client upload form (§3.2); present once stored/served.
    server: ServerMetadata | None = None


def serialized_size_bytes(envelope: Envelope) -> int:
    """UTF-8 byte length of the §3.2 upload wire form ``{body, event_hash}``.

    The cap is defined over the bytes the client uploads (§2.1 "hard reject at
    upload", §3.2 wire form), NOT the full stored envelope — ``signature`` and
    ``server`` are excluded so the measured size is stable for a given ``body``
    regardless of whether server metadata has been attached.
    """
    wire = {
        "body": envelope.body.model_dump(mode="json"),
        "event_hash": envelope.event_hash,
    }
    compact = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)
    return len(compact.encode("utf-8"))


def check_event_size(envelope: Envelope) -> None:
    """Raise :class:`EventTooLargeError` if the upload wire form exceeds the cap."""
    size = serialized_size_bytes(envelope)
    if size > MAX_EVENT_SIZE_BYTES:
        raise EventTooLargeError(size)
