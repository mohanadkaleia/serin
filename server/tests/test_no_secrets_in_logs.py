"""Security AC: raw tokens and passwords never reach the logs (ENG-64 D2)."""

from __future__ import annotations

import io
import logging

from authutil import (
    OWNER,
    accept_invite,
    create_invite,
    do_login,
    do_setup,
    join_token,
)
from httpx import AsyncClient
from msgd.logging import RedactSecretsFilter


async def test_secrets_never_logged(client: AsyncClient) -> None:
    """Drive setup + login + accept-invite while capturing every log line.

    The raw session tokens, the raw invite token, and the passwords must appear
    in no record — message or ``extra``.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(RedactSecretsFilter())  # the same filter the app installs
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        setup_body = await do_setup(client)
        setup_token = setup_body["token"]

        login = await do_login(client, email=OWNER["email"], password=OWNER["password"])
        login_token = login.json()["token"]

        invite = await create_invite(client, setup_token, role="member")
        raw_invite = join_token(invite.json()["url"])
        accepted = await accept_invite(client, raw_invite, email="new@example.com")
        accept_token = accepted.json()["token"]
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    output = buffer.getvalue()
    for secret in (setup_token, login_token, accept_token, raw_invite):
        assert secret not in output, "a raw token leaked into the logs"
    assert OWNER["password"] not in output
    assert "another-valid-password" not in output  # accept-invite default password


def test_redact_filter_scrubs_sensitive_extra() -> None:
    """The redaction filter replaces denylisted ``extra`` keys before formatting."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth event",
        args=None,
        exc_info=None,
    )
    record.token = "raw-bearer-secret"
    record.password = "hunter2-hunter2"
    assert RedactSecretsFilter().filter(record) is True
    assert record.token == "[REDACTED]"  # type: ignore[attr-defined]
    assert record.password == "[REDACTED]"  # type: ignore[attr-defined]


def test_redact_filter_scrubs_token_in_message() -> None:
    """T-SEC-4 (ENG-68 security round 1): a ``token=…`` in the RENDERED message is scrubbed.

    Belt-and-braces backstop, independent of the WS path: even though ENG-68 moved
    the WS token off the URL, no message text (e.g. a URL logged by uvicorn or a
    debug line) may carry a raw token. Covers both a literal message and one built
    from ``%``-args.
    """
    literal = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='connection open GET /v1/ws?token=tok_ABC-123_secret "accepted"',
        args=None,
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(literal) is True
    assert "token=[REDACTED]" in literal.getMessage()
    assert "tok_ABC-123_secret" not in literal.getMessage()

    # The token arriving via %-args must also be scrubbed (args are collapsed).
    formatted = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request %s",
        args=("http://h/v1/ws?token=tok_DEF-456_secret&x=1",),
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(formatted) is True
    assert "tok_DEF-456_secret" not in formatted.getMessage()
    assert "token=[REDACTED]" in formatted.getMessage()


def test_redact_filter_scrubs_ws_subprotocol_bearer() -> None:
    """ENG-73 carryover A: the WS `Sec-WebSocket-Protocol: bearer, <token>` shape is scrubbed.

    ENG-68 moved the WS session token off the URL onto the subprotocol header, but
    the original message scrub only matched the query-string `token=…` form. At
    `uvicorn --log-level debug` the ASGI header list is printed and the token
    surfaces as `sec-websocket-protocol: bearer, <token>` (or a header-tuple
    ``repr``) — a shape the query-string regex misses. Both wire-trace shapes must
    redact to `bearer, [REDACTED]` with the raw token absent.
    """
    raw_token = "wZ8kQx2Lm9Pv4Rt7Nc1Yb3Fj6Hs0Ad5Ge8Ku2Iw4Qo"  # url-safe, 43 chars

    # (a) the plain header-line string form.
    header_line = logging.LogRecord(
        name="uvicorn.error",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg=f"handshake headers: sec-websocket-protocol: bearer, {raw_token}",
        args=None,
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(header_line) is True
    assert raw_token not in header_line.getMessage()
    assert "bearer, [REDACTED]" in header_line.getMessage()

    # (b) the ASGI header-tuple ``repr`` form uvicorn prints in a debug scope dump.
    tuple_repr = logging.LogRecord(
        name="uvicorn.error",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg=repr([(b"sec-websocket-protocol", f"bearer, {raw_token}".encode())]),
        args=None,
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(tuple_repr) is True
    assert raw_token not in tuple_repr.getMessage()
    assert "bearer, [REDACTED]" in tuple_repr.getMessage()

    # A literal "bearer, foo" in prose (short, <16 chars) must NOT be eaten.
    benign = logging.LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth scheme note: bearer, foo tokens are supported",
        args=None,
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(benign) is True
    assert "bearer, foo" in benign.getMessage()
    assert "[REDACTED]" not in benign.getMessage()
