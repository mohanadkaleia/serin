"""Health and metrics endpoints (TDD §4.3 observability).

``/healthz`` performs a real DB ping (``SELECT 1``). ``/metrics`` is a zero-cost
stub returning constant Prometheus text — the real collectors (event
throughput, WS connection count, fanout latency) land with the subsystems they
measure, which do not exist at M1 (ENG-63 D-8). No ``prometheus_client``
dependency: the body is a hand-written constant string.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.db.engine import get_session

router = APIRouter()

# Prometheus exposition format v0.0.4. TODO: replace with real collectors when
# the metered subsystems (events, WS fanout) exist — observability/WS tickets.
_METRICS_STUB = (
    "# HELP msgd_up 1 if the msgd process is serving requests.\n# TYPE msgd_up gauge\nmsgd_up 1\n"
)
_METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/healthz")
async def healthz(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Return 200 ``{"status": "ok"}`` when the DB answers, else 503."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Return the constant Prometheus stub (real metrics deferred, D-8)."""
    return PlainTextResponse(content=_METRICS_STUB, media_type=_METRICS_CONTENT_TYPE)
