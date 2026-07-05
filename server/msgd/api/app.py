"""FastAPI application factory (ENG-63 D-5).

``create_app()`` wires logging, settings, the async engine/session factory, the
lifespan, and the health router. The lifespan does a DB **ping only** — never
DDL: migrations run in the container entrypoint (``python -m msgd.db.migrate``)
before uvicorn boots, and the test harness owns schema via fixtures. This keeps
request serving decoupled from migrations and keeps tests deterministic.

Factory-compatible: ``uvicorn msgd.api.app:create_app --factory``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from msgd.api.routers import health
from msgd.db.engine import create_engine, create_sessionmaker, set_sessionmaker
from msgd.logging import configure_logging
from msgd.settings import Settings, get_settings

logger = logging.getLogger("msgd.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    if settings is None:
        settings = get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        set_sessionmaker(create_sessionmaker(engine))
        # Ping only — no DDL here (migrations run in the entrypoint, D-5).
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("msgd startup complete", extra={"event": "startup"})
        try:
            yield
        finally:
            set_sessionmaker(None)
            await engine.dispose()
            logger.info("msgd shutdown complete", extra={"event": "shutdown"})

    app = FastAPI(title="msgd", lifespan=lifespan)
    app.include_router(health.router)
    return app
