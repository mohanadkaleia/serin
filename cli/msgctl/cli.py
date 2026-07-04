"""``msgctl`` command-line entry point.

Imports :mod:`msgd.core` at module load so the workspace dependency edge from the CLI
to the shared event library is exercised at runtime, not just in tests.
"""

import argparse

import msgd.core  # noqa: F401  -- proves the msgd.core dependency edge at import time

from msgctl import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="msgctl")
    parser.add_argument(
        "--version",
        action="version",
        version=f"msgctl {__version__}",
    )
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
