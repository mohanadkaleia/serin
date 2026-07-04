"""Tests for :mod:`msgd.core.payloads`."""

from __future__ import annotations

import pytest
from msgd.core import ids
from msgd.core.payloads import (
    MessageCreatedV1,
    build_message_created_body,
    get_payload_model,
)
from pydantic import ValidationError


def test_required_fields_and_defaults() -> None:
    msg = MessageCreatedV1(message_id=ids.new_message_id(), text="hi")
    assert msg.format == "markdown"
    assert msg.thread_root_id is None
    assert msg.file_ids == []
    assert msg.mentions == []


def test_missing_required_field_raises() -> None:
    with pytest.raises(ValidationError):
        MessageCreatedV1(text="no message id")  # type: ignore[call-arg]


def test_bad_message_id_prefix_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageCreatedV1(message_id=ids.new_user_id(), text="hi")


def test_non_user_mention_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageCreatedV1(
            message_id=ids.new_message_id(),
            text="hi",
            mentions=[ids.new_message_id()],
        )


def test_non_file_file_id_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageCreatedV1(
            message_id=ids.new_message_id(),
            text="hi",
            file_ids=[ids.new_user_id()],
        )


def test_thread_root_id_must_be_message_id() -> None:
    with pytest.raises(ValidationError):
        MessageCreatedV1(
            message_id=ids.new_message_id(),
            text="hi",
            thread_root_id=ids.new_user_id(),
        )
    # A valid m_ id is accepted.
    root = ids.new_message_id()
    msg = MessageCreatedV1(message_id=ids.new_message_id(), text="reply", thread_root_id=root)
    assert msg.thread_root_id == root


def test_format_literal_domain() -> None:
    assert (
        MessageCreatedV1(message_id=ids.new_message_id(), text="x", format="plain").format
        == "plain"
    )
    with pytest.raises(ValidationError):
        MessageCreatedV1(message_id=ids.new_message_id(), text="x", format="html")  # type: ignore[arg-type]


def test_extra_field_allowed_on_payload() -> None:
    msg = MessageCreatedV1.model_validate(
        {"message_id": ids.new_message_id(), "text": "x", "future_field": 7}
    )
    assert msg.model_dump()["future_field"] == 7


def test_get_payload_model() -> None:
    assert get_payload_model("message.created", 1) is MessageCreatedV1
    assert get_payload_model("message.created", 2) is None
    assert get_payload_model("widget.exploded", 1) is None


def test_build_message_created_body_mints_ids() -> None:
    body = build_message_created_body(
        workspace_id=ids.new_workspace_id(),
        stream_id=ids.new_stream_id(),
        author_user_id=ids.new_user_id(),
        author_device_id=ids.new_device_id(),
        client_created_at="2026-07-04T18:22:10.123Z",
        text="Hello everyone",
    )
    assert body.type == "message.created"
    assert body.type_version == 1
    assert ids.is_valid_ulid(body.event_id)
    assert ids.is_valid_typed_id(body.payload["message_id"], "m_")
    # The payload validates cleanly through the registered model.
    model = get_payload_model(body.type, body.type_version)
    assert model is not None
    model.model_validate(body.payload)
