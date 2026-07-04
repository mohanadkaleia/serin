"""Typed-ULID identifiers for the msg protocol.

Every entity id in msg is a `ULID <https://github.com/ulid/spec>`_ (Crockford
base32, 26 chars, lexicographically sortable) carrying a short type prefix::

    w_  workspace     u_  user       s_  stream
    m_  message       f_  file       d_  device

The one exception is ``event_id`` itself, which per TDD §2.1 is a **bare** ULID
with no prefix.  Ids are client-mintable (offline-safe) because the ULID
timestamp + randomness give global sortability without a server round trip.

Monotonic minting
-----------------
Downstream tickets rely on ULID sort order (there is no ``server_sequence``
client-side).  ``python-ulid``'s default minting draws fresh randomness per
call, so two ids created in the same millisecond can sort in either direction.
:func:`new_ulid` instead wraps a small lock-guarded monotonic factory: within a
millisecond it increments the previous randomness so successive ids are
*strictly* increasing lexicographically and never collide.  A ULID is 16 bytes
(48-bit big-endian millisecond timestamp + 80-bit randomness); we build those
bytes directly and only use the library for base32 encode/decode/validation.

This module must stay free of server-only imports so the CLI can import it
cheaply (TDD §1.1).
"""

from __future__ import annotations

import secrets
import threading
import time
from enum import StrEnum
from typing import Final, NamedTuple

from ulid import ULID

__all__ = [
    "IdKind",
    "ParsedId",
    "ENTITY_PREFIXES",
    "new_ulid",
    "new_event_id",
    "new_typed_id",
    "new_workspace_id",
    "new_user_id",
    "new_stream_id",
    "new_message_id",
    "new_file_id",
    "new_device_id",
    "is_valid_ulid",
    "is_valid_typed_id",
    "parse_typed_id",
]

_TIMESTAMP_BYTES: Final = 6  # 48-bit millisecond timestamp
_RANDOMNESS_BYTES: Final = 10  # 80-bit randomness
_RANDOMNESS_MAX: Final = (1 << (_RANDOMNESS_BYTES * 8)) - 1
_ULID_LENGTH: Final = 26


class IdKind(StrEnum):
    """The entity kinds that carry a type-prefixed ULID."""

    WORKSPACE = "w_"
    USER = "u_"
    STREAM = "s_"
    MESSAGE = "m_"
    FILE = "f_"
    DEVICE = "d_"


ENTITY_PREFIXES: Final[frozenset[str]] = frozenset(kind.value for kind in IdKind)


class ParsedId(NamedTuple):
    """A typed id split into its ``prefix`` and bare ``ulid`` parts."""

    prefix: str
    ulid: str


# --- monotonic minting -------------------------------------------------------

_mint_lock = threading.Lock()
_last_ms: int = -1
_last_randomness: int = 0


def new_ulid() -> str:
    """Return a fresh 26-char ULID, strictly increasing across calls.

    Lock-guarded and monotonic: two ids minted in the same millisecond differ by
    an incremented randomness, so ``new_ulid() < new_ulid()`` always holds.
    """
    global _last_ms, _last_randomness

    with _mint_lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _last_randomness = secrets.randbits(_RANDOMNESS_BYTES * 8)
        else:
            # Same millisecond (or a backwards clock): keep the timestamp and
            # bump the randomness so the id still strictly increases.
            _last_randomness += 1
            if _last_randomness > _RANDOMNESS_MAX:
                # Randomness overflow within a millisecond (astronomically
                # unlikely): carry into the timestamp to stay monotonic.
                _last_ms += 1
                _last_randomness = secrets.randbits(_RANDOMNESS_BYTES * 8)

        raw = _last_ms.to_bytes(_TIMESTAMP_BYTES, "big") + _last_randomness.to_bytes(
            _RANDOMNESS_BYTES, "big"
        )

    return str(ULID(raw))


def new_event_id() -> str:
    """Return a bare ULID for use as an ``event_id`` (no prefix, per §2.1)."""
    return new_ulid()


def new_typed_id(prefix: str) -> str:
    """Return ``prefix + <ULID>`` for a known entity prefix (e.g. ``"m_"``)."""
    if prefix not in ENTITY_PREFIXES:
        raise ValueError(f"unknown entity prefix: {prefix!r}")
    return prefix + new_ulid()


def new_workspace_id() -> str:
    return new_typed_id(IdKind.WORKSPACE)


def new_user_id() -> str:
    return new_typed_id(IdKind.USER)


def new_stream_id() -> str:
    return new_typed_id(IdKind.STREAM)


def new_message_id() -> str:
    return new_typed_id(IdKind.MESSAGE)


def new_file_id() -> str:
    return new_typed_id(IdKind.FILE)


def new_device_id() -> str:
    return new_typed_id(IdKind.DEVICE)


# --- parse / validate --------------------------------------------------------


def is_valid_ulid(value: str) -> bool:
    """True if ``value`` is a syntactically valid bare ULID (26-char base32)."""
    if len(value) != _ULID_LENGTH:
        return False
    try:
        ULID.from_str(value)
    except (ValueError, TypeError):
        return False
    return True


def is_valid_typed_id(value: str, prefix: str) -> bool:
    """True if ``value`` is ``prefix`` followed by a valid ULID."""
    if not value.startswith(prefix):
        return False
    return is_valid_ulid(value[len(prefix) :])


def parse_typed_id(value: str, *, expected_prefix: str | None = None) -> ParsedId:
    """Split a typed id into ``(prefix, ulid)``.

    Raises :class:`ValueError` if the prefix is not a known entity prefix (or,
    when given, does not equal ``expected_prefix``) or the remainder is not a
    valid ULID.
    """
    if expected_prefix is not None:
        if not value.startswith(expected_prefix):
            raise ValueError(f"expected prefix {expected_prefix!r}, got id {value!r}")
        prefix = expected_prefix
    else:
        prefix = next((p for p in ENTITY_PREFIXES if value.startswith(p)), "")
        if not prefix:
            raise ValueError(f"id {value!r} has no known entity prefix")

    ulid = value[len(prefix) :]
    if not is_valid_ulid(ulid):
        raise ValueError(f"id {value!r} does not contain a valid ULID")
    return ParsedId(prefix=prefix, ulid=ulid)
