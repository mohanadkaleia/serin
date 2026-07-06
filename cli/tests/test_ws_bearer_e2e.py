"""Real-uvicorn WS bearer-subprotocol regression E2E (ENG-92).

This is the test that the ENG-68 in-process WS suite could not be: it drives a
**real** uvicorn subprocess (not the Starlette/httpx in-process ASGI transport) and
connects with a **real** ``websockets`` client. That distinction is the whole bug —
uvicorn's default ``websockets`` sans-io backend surfaces
``Sec-WebSocket-Protocol: bearer, <token>`` to ASGI as the *un-split* single element
``["bearer, <token>"]``, whereas the in-process transport (and ``wsproto``) pre-split
it into ``["bearer", "<token>"]``. The old extractor required ``len >= 2`` and so
403'd a valid token under the default backend while every in-process test stayed
green.

We parametrize the WS backend over ``["auto", "wsproto"]``:

* ``auto`` is uvicorn's default (the ``websockets`` sans-io impl) — the exact backend
  that shipped broken. This case is RED without the ``_bearer_token`` flatten fix and
  GREEN with it.
* ``wsproto`` is the backend the container entrypoint now pins (``--ws wsproto``) and
  the shape the WS e2e was validated against.

Marked ``integration`` (needs Docker for the Postgres testcontainer); ``-m "not
integration"`` skips it. Reuses ``_e2e_server``'s ``_free_port`` / ``_wait_healthy``
so the real-server plumbing lives in one place.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import websockets
from _e2e_server import _free_port, _wait_healthy
from msgctl import credentials
from msgctl.workspace import Workspace
from msgd.db.migrate import run_migrations
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

pytestmark = pytest.mark.integration

OWNER_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(params=["auto", "wsproto"], scope="module")
def ws_server(request: pytest.FixtureRequest) -> Iterator[tuple[str, Path, str]]:
    """Postgres testcontainer + subprocess uvicorn on the true ASGI app.

    Parametrized over the uvicorn WS backend so the regression is proven against BOTH
    the default ``websockets`` backend (``auto`` — previously broken) and the pinned
    ``wsproto`` backend. Each param gets its own container (server ``setup`` is
    one-time per DB). Bootstraps the owner once and yields ``(base_url,
    workspace_dir, token)``.
    """
    backend: str = request.param
    tmp_path_factory: pytest.TempPathFactory = request.getfixturevalue("tmp_path_factory")
    with PostgresContainer("postgres:17") as container:
        raw_url: str = container.get_connection_url()
        asyncpg_url = raw_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
        run_migrations(asyncpg_url)

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        data_dir = tmp_path_factory.mktemp(f"msgd-data-{backend}")
        log_path = tmp_path_factory.mktemp(f"msgd-logs-{backend}") / "server.log"
        workspace_dir = tmp_path_factory.mktemp(f"msgd-ws-{backend}") / "owner"

        env = {
            **os.environ,
            "MSG_DATABASE_URL": asyncpg_url,
            "MSG_DATA_DIR": str(data_dir),
            "MSG_SECRET_KEY": "e2e-ws-secret",
            "MSG_LOG_LEVEL": "INFO",
            "MSG_DOCS_ENABLED": "false",
            "MSG_ARGON2_TIME_COST": "1",
            "MSG_ARGON2_MEMORY_COST_KIB": "8",
            "MSG_ARGON2_PARALLELISM": "1",
        }
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "msgd.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--ws",
                    backend,
                    "--log-level",
                    "info",
                ],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_healthy(base_url)
                token = _setup_owner(base_url, workspace_dir)
                yield base_url, workspace_dir, token
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)


def _msgctl(*args: str) -> str:
    """Invoke ``msgctl`` as a real subprocess; assert success; return stdout."""
    result = subprocess.run(
        [sys.executable, "-m", "msgctl.cli", *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"`msgctl {' '.join(args)}` failed: {result.stderr}"
    return result.stdout.strip()


def _setup_owner(base_url: str, workspace_dir: Path) -> str:
    """Bootstrap owner+workspace (creates the ``general`` channel); return the raw token."""
    _msgctl(
        "login",
        str(workspace_dir),
        "--setup",
        "--server-url",
        base_url,
        "--email",
        "owner@example.com",
        "--password",
        OWNER_PASSWORD,
        "--workspace-name",
        "Acme",
        "--display-name",
        "Owner",
    )
    creds_dir = credentials.msgctl_dir(Workspace.open(workspace_dir))
    creds_path = creds_dir / credentials.CREDENTIALS_NAME
    token: str = json.loads(creds_path.read_text())["token"]
    assert token
    return token


def _ws_url(base_url: str) -> str:
    return base_url.replace("http://", "ws://", 1) + "/v1/ws"


def _bearer(token: str) -> list[Subprotocol]:
    """The ``Sec-WebSocket-Protocol`` offer list carrying a bearer ``token``."""
    return [Subprotocol("bearer"), Subprotocol(token)]


async def _read_until(ws: Any, t: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Receive frames until one with ``{"t": t}`` arrives (skips heartbeat noise)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.01))
        msg = json.loads(raw)
        if isinstance(msg, dict) and msg.get("t") == t:
            return msg


async def test_valid_bearer_subprotocol_upgrades_and_fans_out(
    ws_server: tuple[str, Path, str],
) -> None:
    """Valid token via ``Sec-WebSocket-Protocol: bearer, <token>`` → 101 + a fanout event.

    This is the M2 sync proof at the WS surface: a real browser-shaped handshake
    against a real uvicorn upgrades, the server echoes the ``bearer`` subprotocol, and
    a message posted over HTTP arrives as a ``{"t": "event"}`` frame.
    """
    base_url, workspace_dir, token = ws_server

    async with websockets.connect(
        _ws_url(base_url), subprotocols=_bearer(token), open_timeout=10
    ) as ws:
        # The offered subprotocol MUST be echoed or a real browser aborts the handshake.
        assert ws.subprotocol == "bearer"

        # Registration barrier: a pong proves the socket is in the hub before we push,
        # so the fanout cannot race ahead of registration.
        await ws.send(json.dumps({"t": "ping"}))
        await _read_until(ws, "pong")

        # Publish over HTTP (send stages, push uploads → server fans out on commit).
        sent = json.loads(
            await asyncio.to_thread(
                _msgctl, "send", str(workspace_dir), "--stream", "general", "--text", "hello-ws"
            )
        )
        event_id = sent["event_id"]
        await asyncio.to_thread(_msgctl, "push", str(workspace_dir))

        # Read event frames until our just-published message fans back out.
        while True:
            frame = await _read_until(ws, "event")
            body = frame["event"]["body"]
            if body["event_id"] == event_id:
                break
        assert body["payload"]["text"] == "hello-ws"
        assert isinstance(frame["event"]["server"]["server_sequence"], int)


async def test_absent_token_rejected_403(ws_server: tuple[str, Path, str]) -> None:
    """No ``Sec-WebSocket-Protocol`` at all → the handshake is rejected (HTTP 403)."""
    base_url, _workspace_dir, _token = ws_server
    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(_ws_url(base_url), open_timeout=10):
            pass
    assert excinfo.value.response.status_code == 403


async def test_invalid_token_rejected_403(ws_server: tuple[str, Path, str]) -> None:
    """A bearer subprotocol carrying a bogus token → rejected (HTTP 403), never accepted."""
    base_url, _workspace_dir, _token = ws_server
    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(
            _ws_url(base_url), subprotocols=_bearer("not-a-real-token"), open_timeout=10
        ):
            pass
    assert excinfo.value.response.status_code == 403
