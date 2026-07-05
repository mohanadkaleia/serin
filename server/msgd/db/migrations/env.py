"""Alembic environment — async engine (TDD §4.1, ENG-63 D-5).

Online migrations run through the async engine to match the app engine. The
sync ``alembic.command`` API drives this via ``connection.run_sync`` inside an
``asyncio.run`` bridge. ``target_metadata`` is the ORM ``Base.metadata`` so
autogenerate / ``compare_metadata`` see the full §4.2 schema. The database URL
is taken from ``MSG_DATABASE_URL`` (falling back to the config value), never the
hard-coded placeholder in ``alembic.ini``.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

# Import the models module for its side effect: registering every table on
# Base.metadata so autogenerate/compare_metadata see the full schema.
import msgd.db.models  # noqa: F401
from alembic import context
from msgd.db.base import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL resolution order: an explicit URL set on the Config (by
# migrate.run_migrations) wins; otherwise fall back to MSG_DATABASE_URL. The
# hard-coded placeholder in alembic.ini is never used against a real database.
_configured = config.get_main_option("sqlalchemy.url") or ""
if not _configured or "placeholder" in _configured:
    _env_url = os.environ.get("MSG_DATABASE_URL")
    if _env_url:
        config.set_main_option("sqlalchemy.url", _env_url.replace("%", "%%"))

target_metadata = Base.metadata


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
