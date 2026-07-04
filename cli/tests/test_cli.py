"""Tests for the CLI member (``msgctl``)."""

from types import ModuleType

import pytest
from msgctl import __version__
from msgctl.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"msgctl {__version__}"


def test_core_importable_from_cli() -> None:
    import msgctl
    import msgd.core

    assert isinstance(msgctl, ModuleType)
    assert isinstance(msgd.core, ModuleType)
