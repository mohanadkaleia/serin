"""Tests for :mod:`msgd.core.ids`."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st
from msgd.core import ids

_PREFIXED_FACTORIES: dict[str, Callable[[], str]] = {
    "w_": ids.new_workspace_id,
    "u_": ids.new_user_id,
    "s_": ids.new_stream_id,
    "m_": ids.new_message_id,
    "f_": ids.new_file_id,
    "d_": ids.new_device_id,
}


@pytest.mark.parametrize("prefix,factory", list(_PREFIXED_FACTORIES.items()))
def test_prefixed_factory_shape(prefix: str, factory: Callable[[], str]) -> None:
    value = factory()
    assert value.startswith(prefix)
    assert ids.is_valid_typed_id(value, prefix)
    assert ids.is_valid_ulid(value[len(prefix) :])


def test_event_id_is_bare_ulid() -> None:
    value = ids.new_event_id()
    assert ids.is_valid_ulid(value)
    assert not any(value.startswith(p) for p in ids.ENTITY_PREFIXES)


def test_new_typed_id_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError):
        ids.new_typed_id("x_")


def test_monotonic_strictly_increasing() -> None:
    minted = [ids.new_ulid() for _ in range(10_000)]
    assert len(set(minted)) == len(minted), "ULIDs must be unique"
    assert minted == sorted(minted), "ULIDs must be strictly increasing"


def test_monotonic_across_prefixed_ids() -> None:
    minted = [ids.new_message_id() for _ in range(1000)]
    assert minted == sorted(minted)
    assert len(set(minted)) == len(minted)


def test_is_valid_ulid_rejects_bad_values() -> None:
    assert not ids.is_valid_ulid("")
    assert not ids.is_valid_ulid("tooshort")
    assert not ids.is_valid_ulid("0" * 25)  # wrong length
    assert not ids.is_valid_ulid("0" * 27)
    # 'I', 'L', 'O', 'U' are not Crockford base32 characters.
    assert not ids.is_valid_ulid("I" * 26)


def test_is_valid_typed_id() -> None:
    mid = ids.new_message_id()
    assert ids.is_valid_typed_id(mid, "m_")
    assert not ids.is_valid_typed_id(mid, "u_")  # wrong prefix
    assert not ids.is_valid_typed_id("m_tooshort", "m_")  # bad ulid


def test_parse_typed_id_ok() -> None:
    mid = ids.new_message_id()
    parsed = ids.parse_typed_id(mid)
    assert parsed.prefix == "m_"
    assert mid == parsed.prefix + parsed.ulid
    assert ids.parse_typed_id(mid, expected_prefix="m_").prefix == "m_"


def test_parse_typed_id_rejects() -> None:
    mid = ids.new_message_id()
    with pytest.raises(ValueError):
        ids.parse_typed_id(mid, expected_prefix="u_")  # prefix mismatch
    with pytest.raises(ValueError):
        ids.parse_typed_id("01JZ7N6A4M6Y8W5K2H7DGKX4PA")  # no prefix
    with pytest.raises(ValueError):
        ids.parse_typed_id("m_notavalidulidgoeshere00")  # bad ulid


@given(prefix=st.sampled_from(sorted(ids.ENTITY_PREFIXES)))
def test_parse_round_trip_all_prefixes(prefix: str) -> None:
    value = ids.new_typed_id(prefix)
    assert ids.parse_typed_id(value).prefix == prefix
