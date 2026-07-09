"""``python -m github_notifier`` — boot the notifier from the environment."""

from __future__ import annotations

from github_notifier.server import main

if __name__ == "__main__":
    raise SystemExit(main())
