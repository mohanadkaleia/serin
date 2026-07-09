"""``pull_request`` payload -> a one-line plain-text message (or ``None``).

The msg hook stores whatever it receives as ``format="plain"`` (the ENG-161
injection guard), so nothing here needs escaping — GitHub-controlled strings
(title, logins) arrive in the channel as inert characters.

Handled actions: ``opened``, ``closed`` (split into merged vs closed via
``pull_request.merged``), and ``review_requested``. Everything else returns
``None`` — the caller acks GitHub with a 200 and posts nothing.
"""

from __future__ import annotations

from typing import Any

__all__ = ["format_pull_request"]


def _login(obj: object) -> str:
    """A GitHub actor object's ``login``, defensively (``"unknown"`` on any miss)."""
    if isinstance(obj, dict):
        login = obj.get("login")
        if isinstance(login, str) and login:
            return login
    return "unknown"


def format_pull_request(payload: dict[str, Any]) -> str | None:
    """Format one ``pull_request`` event payload, or ``None`` to ignore it.

    Defensive throughout: a payload missing the PR number/title/URL (or any
    unhandled ``action``) yields ``None`` rather than a malformed message —
    an odd delivery is dropped, never garbled into the channel.
    """
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return None
    number = pr.get("number")
    title = pr.get("title")
    url = pr.get("html_url")
    if not (isinstance(number, int) and isinstance(title, str) and isinstance(url, str)):
        return None

    action = payload.get("action")
    if action == "opened":
        return f"PR #{number} opened by {_login(pr.get('user'))}: {title} — {url}"
    if action == "closed":
        verb = "merged" if pr.get("merged") is True else "closed"
        return f"PR #{number} {verb} by {_login(payload.get('sender'))}: {title} — {url}"
    if action == "review_requested":
        reviewer = _login(payload.get("requested_reviewer"))
        actor = _login(payload.get("sender"))
        return f"PR #{number} review requested from {reviewer} by {actor}: {title} — {url}"
    return None
