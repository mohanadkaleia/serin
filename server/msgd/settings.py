"""Environment-driven application settings (TDD §4.1).

All configuration is read from ``MSG_``-prefixed environment variables via
``pydantic-settings``. :func:`get_settings` is cached so a single ``Settings``
instance is shared process-wide; tests clear the cache when they override env.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, populated from ``MSG_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="MSG_", extra="ignore")

    # asyncpg DSN, e.g. ``postgresql+asyncpg://user:pass@host:5432/msg``.
    database_url: str
    # Root directory for blob storage and other on-disk state (§4.3 backups).
    data_dir: Path
    # Secret used for session-token / signing material by later tickets.
    secret_key: str
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
