"""Smoke tests for the server member (``msgd``)."""

from types import ModuleType


def test_placeholder() -> None:
    assert True


def test_core_importable_from_server() -> None:
    import msgd.core

    assert isinstance(msgd.core, ModuleType)
