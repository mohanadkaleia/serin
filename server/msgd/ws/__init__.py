"""In-process WebSocket surface (ENG-68 / M1, TDD §3.3).

``GET /v1/ws`` authenticates a socket, registers it in the process-global
:data:`~msgd.ws.hub.hub`, and runs a ping/pong heartbeat; the
:func:`msgd.events.fanout.publish_event` seam delegates to ``hub.publish`` for
permission-scoped, per-send fanout. Single worker, no shared pub/sub (§11/§14).
"""

from __future__ import annotations

from msgd.ws.hub import hub
from msgd.ws.router import router

__all__ = ["hub", "router"]
