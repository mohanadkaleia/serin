"""Programmatic Alembic upgrade (ENG-63 D-5).

Migrations run in the container entrypoint *before* uvicorn boots and in the
test harness once per session — never in the app lifespan (which does a DB ping
only, no DDL). Both call :func:`run_migrations`, which builds an Alembic
``Config`` in code (``script_location`` resolved relative to this package, so it
is cwd-independent and works identically inside the image and in tests) and runs
``alembic upgrade head``. The async engine bridge lives in ``migrations/env.py``.

Run standalone via ``python -m msgd.db.migrate`` (uses ``MSG_DATABASE_URL``).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from msgd.logging import configure_logging
from msgd.settings import get_settings

_PACKAGE_DIR = Path(__file__).resolve().parent
_SCRIPT_LOCATION = _PACKAGE_DIR / "migrations"
_ALEMBIC_INI = _PACKAGE_DIR.parent.parent / "alembic.ini"


def make_config(url: str | None = None) -> Config:
    """Build an Alembic ``Config`` with an absolute, cwd-independent script path."""
    cfg = Config(str(_ALEMBIC_INI) if _ALEMBIC_INI.exists() else None)
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    if url is not None:
        # ConfigParser interpolation treats %; escape it for odd passwords.
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def run_migrations(url: str | None = None) -> None:
    """Upgrade the database at ``url`` (or ``MSG_DATABASE_URL``) to ``head``."""
    if url is None:
        url = get_settings().database_url
    cfg = make_config(url)
    command.upgrade(cfg, "head")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    run_migrations(settings.database_url)


if __name__ == "__main__":
    main()
