"""The stdlib HTTP wrapper + ``main()`` entry point (``python -m github_notifier``).

A :class:`~http.server.ThreadingHTTPServer` whose POST handler reads the raw
body (Content-Length-bounded) and hands it, with the three GitHub headers, to
:meth:`github_notifier.notifier.Notifier.handle`. ``GET /healthz`` answers
``200 {"ok": true}`` so a supervisor (the M5 exit gate boots this module as a
subprocess) can probe readiness.

``main()`` is start/stop clean: it prints ``listening on <host>:<port>`` once
the socket is bound (port ``0`` resolves to the real ephemeral port), serves in
a background thread, and shuts the server down on SIGINT/SIGTERM, exiting 0.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from github_notifier.config import Config, ConfigError, load_config
from github_notifier.notifier import Notifier, Response

__all__ = ["build_server", "main"]

#: Inbound body cap. Real ``pull_request`` payloads are tens of KB; anything
#: bigger is dropped before it is read (GitHub itself caps payloads at 25 MB).
MAX_BODY_BYTES = 1024 * 1024


def build_server(config: Config, notifier: Notifier | None = None) -> ThreadingHTTPServer:
    """Bind and return the (not yet serving) HTTP server for ``config``."""
    active = notifier if notifier is not None else Notifier(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "github-notifier"

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self._respond(Response(411, {"error": "Content-Length is required"}))
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._respond(Response(413, {"error": "request body too large"}))
                return
            raw_body = self.rfile.read(length)
            self._respond(
                active.handle(
                    event=self.headers.get("X-GitHub-Event"),
                    delivery_id=self.headers.get("X-GitHub-Delivery"),
                    signature=self.headers.get("X-Hub-Signature-256"),
                    raw_body=raw_body,
                )
            )

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
            if self.path == "/healthz":
                self._respond(Response(200, {"ok": True}))
            else:
                self._respond(Response(404, {"error": "not found"}))

        def _respond(self, response: Response) -> None:
            body = json.dumps(response.body).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Silence per-request stderr logging (outcomes are the JSON answers)."""

    return ThreadingHTTPServer((config.host, config.port), Handler)


def main() -> int:
    """Load config from the environment, serve until SIGINT/SIGTERM, exit clean."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"github-notifier: {exc}", file=sys.stderr)
        return 2

    server = build_server(config)
    stop = threading.Event()

    def _request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    # serve_forever runs in a worker thread; the main thread waits on the stop
    # event. (Calling server.shutdown() from a signal handler inside the serving
    # thread would deadlock — shutdown() blocks until the serve loop exits.)
    thread = threading.Thread(target=server.serve_forever, name="github-notifier", daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    print(f"github-notifier: listening on {host!s}:{port}", flush=True)

    stop.wait()
    print("github-notifier: shutting down", flush=True)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    return 0
