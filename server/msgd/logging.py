"""Structured JSON logging (TDD §4.3 observability).

A dependency-free ``logging.dictConfig`` setup that emits one JSON object per
line and routes uvicorn's ``access`` and ``error`` loggers through the same
formatter so app and server logs share a shape. ``configure_logging`` is called
by both :func:`msgd.api.app.create_app` and :mod:`msgd.db.migrate`; the
container entrypoint passes ``--log-config`` / ``log_config=None`` to uvicorn so
this config wins over uvicorn's defaults.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from logging.config import dictConfig
from typing import Any

_RESERVED = frozenset(
    logging.makeLogRecord({}).__dict__.keys() | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Merge any structured `extra=` fields the caller attached.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on root + uvicorn loggers at ``level``."""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": "msgd.logging.JsonFormatter"}},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["default"], "level": level},
            "loggers": {
                # Route uvicorn through our handler; don't let it double-log.
                "uvicorn": {"handlers": ["default"], "level": level, "propagate": False},
                "uvicorn.error": {
                    "handlers": ["default"],
                    "level": level,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["default"],
                    "level": level,
                    "propagate": False,
                },
            },
        }
    )
