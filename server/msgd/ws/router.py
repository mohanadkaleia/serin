"""``GET /v1/ws`` — the authenticated WebSocket surface: fanout target + heartbeat.

Flow (§7):

1. **Auth, pre-accept.** Read ``?token=…`` (WS clients cannot set ``Authorization``,
   §3.3). Resolve it exactly as ``require_auth`` does — ``hash_token`` +
   ``lookup_session`` + the same expiry/deactivation checks + the throttled
   ``bump_session`` (D4) — using the standard, test-overridable ``get_session``.
   Any failure closes the socket **before** ``accept()`` with app code ``4401``
   (uniform, non-disclosing). The session-loading is re-called inline rather than
   factored out of the merged ``deps.require_auth``.
2. **Accept + register (cap, §5).** ``accept()`` then register in the hub. Over the
   per-user cap → accept-then-close ``4029`` (so the client receives the code).
3. **Serve.** Two concurrent coroutines for the socket's lifetime — a tolerant
   receive loop (client ``ping`` → server ``pong``; client ``pong`` clears the
   outstanding-ping flag; every other frame ignored, D9) and a 30 s heartbeat
   (send ``ping``; if the previous one is still unanswered, close ``4408``).
   Whichever ends first tears the other down; a single ``finally`` deregisters.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from msgd.auth.sessions import bump_session, lookup_session, utcnow
from msgd.auth.tokens import hash_token
from msgd.db.engine import get_session
from msgd.settings import Settings
from msgd.ws.frames import PING, PONG, WSCloseCode
from msgd.ws.hub import hub
from msgd.ws.registry import Connection

__all__ = ["router"]

router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_session)]


class _HeartbeatState:
    """Shared liveness flag between the receive loop and the heartbeat loop."""

    __slots__ = ("awaiting_pong",)

    def __init__(self) -> None:
        self.awaiting_pong = False


@router.websocket("/v1/ws")
async def websocket_endpoint(websocket: WebSocket, db: DbSession) -> None:
    """Authenticate, register, and serve one client socket (see the module docstring)."""
    settings: Settings = websocket.app.state.settings

    connection = await _authenticate(websocket, db, settings)
    if connection is None:
        return  # already closed pre-accept with 4401

    await websocket.accept()
    if not hub.try_register(connection, max_connections=settings.ws_max_connections_per_user):
        # Accept-then-close so the client receives the 4029 close frame (§5).
        await websocket.close(code=WSCloseCode.TOO_MANY_CONNECTIONS)
        return

    try:
        await _serve(websocket, settings)
    finally:
        hub.deregister(connection)


async def _authenticate(
    websocket: WebSocket, db: AsyncSession, settings: Settings
) -> Connection | None:
    """Resolve the query token → :class:`Connection`, or close ``4401`` and return None.

    Uniform, non-disclosing failure (missing/unknown token, expired session,
    deactivated user) — same discipline as ``require_auth`` — always closed
    **before** ``accept()`` so no unauthenticated socket is ever accepted.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=WSCloseCode.UNAUTHENTICATED)
        return None

    loaded = await lookup_session(db, hash_token(token))
    if loaded is None:
        await websocket.close(code=WSCloseCode.UNAUTHENTICATED)
        return None
    session, user, device = loaded

    now = utcnow()
    if now >= session.expires_at or user.deactivated_at is not None:
        await websocket.close(code=WSCloseCode.UNAUTHENTICATED)
        return None

    # Throttled rolling bump for parity with require_auth (D4); the overridden
    # get_session is savepoint-isolated in tests, so a bump-commit lands on a
    # savepoint, not the outer per-test transaction (R8).
    if await bump_session(db, session, settings=settings, now=now):
        await db.commit()

    return Connection(
        websocket=websocket,
        user_id=user.user_id,
        role=user.role,
        workspace_id=user.workspace_id,
        device_id=device.device_id,
    )


async def _serve(websocket: WebSocket, settings: Settings) -> None:
    """Run the receive loop + heartbeat concurrently; first to finish tears down."""
    state = _HeartbeatState()
    receive_task = asyncio.create_task(_receive_loop(websocket, state))
    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket, settings, state))
    tasks = (receive_task, heartbeat_task)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _receive_loop(websocket: WebSocket, state: _HeartbeatState) -> None:
    """Tolerant inbound loop: ``ping`` → ``pong``, ``pong`` clears the flag, else ignore.

    Any non-text / non-JSON / unknown frame is dropped without crashing (D9). Exits
    cleanly on client disconnect.
    """
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except (RuntimeError, KeyError):
            # A binary frame (no "text") or a post-disconnect receive → ignore.
            continue
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(message, dict):
            continue
        t = message.get("t")
        if t == "ping":
            with contextlib.suppress(Exception):
                await websocket.send_json(PONG)
        elif t == "pong":
            state.awaiting_pong = False
        # Every other / reserved (M3) / unknown frame is ignored (D9 tolerance).


async def _heartbeat_loop(websocket: WebSocket, settings: Settings, state: _HeartbeatState) -> None:
    """Every interval: if the prior ping is unanswered close ``4408``; else send ``ping``."""
    interval = settings.ws_heartbeat_interval_seconds
    while True:
        await asyncio.sleep(interval)
        if state.awaiting_pong:
            with contextlib.suppress(Exception):
                await websocket.close(code=WSCloseCode.HEARTBEAT_TIMEOUT)
            return
        try:
            await websocket.send_json(PING)
        except Exception:
            return
        state.awaiting_pong = True
