#!/usr/bin/env sh
# msgd container entrypoint (ENG-63 D-5, TDD §11).
#
# Migrate to head, THEN serve. Migrations run here — once, before uvicorn boots
# — never in the app lifespan (which does a DB ping only). This keeps request
# serving decoupled from DDL and gives `docker compose up` a single deterministic
# place to apply schema.
#
# ONE WORKER, deliberately (--workers 1): M1 keeps the WebSocket registry and
# fanout hub in-process (in-memory), so a second worker would not see the first
# worker's connections. Horizontal scale needs a shared pub/sub (Redis/NATS),
# which is explicitly out of MVP scope (TDD §11 / §4.1: "no Redis, no queue").
# Do not raise the worker count without adding a shared fanout bus first.
set -e

python -m msgd.db.migrate

# create_app() calls configure_logging() (msgd/logging.py) as the factory runs,
# which reconfigures uvicorn's own loggers (disable_existing_loggers=False,
# propagate=False) so our JSON formatter wins over uvicorn's defaults.
#
# WebSocket backend: uvicorn's DEFAULT (`--ws auto` -> the `websockets` sans-io
# impl) is the only WS backend `uvicorn[standard]` ships. wsproto reaches ONLY the
# dev tree (via httpx-ws) and is excluded by the image's `uv sync --no-dev`, so a
# `--ws wsproto` pin would crash the container on boot. ENG-92: the default backend
# surfaces `Sec-WebSocket-Protocol: bearer, <token>` to ASGI *un-split* as the
# single element ["bearer, <token>"]; the `_bearer_token` extractor
# (msgd/ws/router.py) now normalizes that shape, so the shipped default backend
# authenticates the bearer subprotocol correctly with no flag.
exec uvicorn msgd.api.app:create_app \
  --factory \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 1
