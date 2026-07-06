"""Request/response shapes for ``POST /v1/events/batch`` (TDD §3.2, ENG-66).

The batch endpoint answers with a per-event ``accepted`` / ``rejected`` split
(never a single all-or-nothing status): a well-formed request always returns
200 with these two arrays, and only *batch-level* violations (body >1 MB,
>100 events, malformed top-level JSON) short-circuit to a problem+json.

**Naming-collision note (deliberate — keep both):** the per-event rejection
*code* :data:`RejectionCode` value ``payload_too_large`` (the 64 KB single-event
wire-form cap, surfaced in ``rejected[]``) is a different layer from the
batch-level ``/problems/payload-too-large`` (1 MB whole-body cap, a 413
problem+json in :mod:`msgd.api.problems`). One is a per-item outcome, the other
rejects the entire request; they are distinct on purpose.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

__all__ = [
    "RejectionCode",
    "AcceptedEvent",
    "RejectedEvent",
    "BatchUploadResponse",
    "BatchUploadRequest",
]

#: The five §3.2 per-event rejection codes. Exhaustive and closed: every reject
#: path in :mod:`msgd.events.validate` maps to exactly one of these.
RejectionCode = Literal[
    "permission_denied",
    "invalid_schema",
    "hash_mismatch",
    "payload_too_large",
    "unknown_stream",
]


class AcceptedEvent(BaseModel):
    """One accepted event's server-assigned coordinates (§3.2 ``accepted[]``).

    Exactly these four fields — *not* the full stored envelope. On the
    idempotent re-accept path they reproduce the **original** acceptance's four
    values (D7), including ``server_received_at`` re-rendered from the stored
    timestamp with the same millisecond-``Z`` truncation.
    """

    event_id: str
    stream_id: str
    server_sequence: int
    server_received_at: str  # RFC3339 millisecond-Z, from Envelope.server


class RejectedEvent(BaseModel):
    """One rejected event: the (best-effort) id, a closed code, and a detail."""

    #: ``""`` when the item is so malformed no ``event_id`` can be read.
    event_id: str
    code: RejectionCode
    detail: str


class BatchUploadResponse(BaseModel):
    """The §3.2 batch response: a partition of the input into two arrays."""

    accepted: list[AcceptedEvent]
    rejected: list[RejectedEvent]


class BatchUploadRequest(BaseModel):
    """Docs-only request shape for ``POST /v1/events/batch``.

    **The handler does NOT bind this model.** It reads the raw request body and
    parses it once (D2), because the exact client bytes of each ``body`` are the
    sole input to ``hash_event`` and must never pass through a Pydantic
    round-trip before hashing (the ENG-56 lax-coercion hazard). This model exists
    only to document the wire shape for OpenAPI; ``events`` items are raw
    ``{"body": {...}, "event_hash": "..."}`` dicts.
    """

    events: list[dict[str, Any]]
