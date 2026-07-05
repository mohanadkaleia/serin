"""/healthz and /metrics endpoint tests (ENG-63 acceptance #2, D-8)."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_ok(client: AsyncClient) -> None:
    """/healthz returns 200 {"status": "ok"} against the real migrated DB."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_metrics_stub(client: AsyncClient) -> None:
    """/metrics returns the constant Prometheus stub in exposition format."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]
    assert "msgd_up 1" in resp.text
