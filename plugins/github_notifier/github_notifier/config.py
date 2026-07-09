"""Environment configuration — fail fast, with a message that names the variable.

The plugin is meant to be booted as a subprocess (the M5 exit gate does exactly
that), so a missing/malformed variable must be an immediate, legible startup
failure — never a server that comes up and then drops every delivery.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ENV_HOOK_URL",
    "ENV_HOST",
    "ENV_PORT",
    "ENV_SECRET",
    "Config",
    "ConfigError",
    "load_config",
]

ENV_SECRET = "GITHUB_WEBHOOK_SECRET"
ENV_HOOK_URL = "MSG_HOOK_URL"
ENV_HOST = "GITHUB_NOTIFIER_HOST"
ENV_PORT = "GITHUB_NOTIFIER_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8477


class ConfigError(Exception):
    """A required environment variable is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """The notifier's full runtime configuration (immutable once loaded)."""

    #: The GitHub webhook shared secret — HMAC key for ``X-Hub-Signature-256``.
    webhook_secret: bytes
    #: The msg incoming-webhook capability URL (``…/v1/hooks/<hook_token>``).
    hook_url: str
    #: Bind address for the inbound GitHub-webhook listener.
    host: str
    port: int


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (default ``os.environ``), else raise.

    Every failure is a :class:`ConfigError` whose message names the offending
    variable — the ``main()`` entry point prints it and exits nonzero.
    """
    if env is None:
        env = os.environ
    secret = env.get(ENV_SECRET, "")
    if not secret:
        raise ConfigError(f"{ENV_SECRET} is required (the GitHub webhook shared secret)")
    hook_url = env.get(ENV_HOOK_URL, "")
    if not hook_url:
        raise ConfigError(f"{ENV_HOOK_URL} is required (the msg incoming-webhook capability URL)")
    if not hook_url.startswith(("http://", "https://")):
        raise ConfigError(f"{ENV_HOOK_URL} must be an http:// or https:// URL, got {hook_url!r}")
    host = env.get(ENV_HOST, DEFAULT_HOST)
    raw_port = env.get(ENV_PORT, str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(f"{ENV_PORT} must be an integer, got {raw_port!r}") from None
    if not 0 <= port <= 65535:
        raise ConfigError(f"{ENV_PORT} must be in [0, 65535], got {port}")
    return Config(webhook_secret=secret.encode("utf-8"), hook_url=hook_url, host=host, port=port)
