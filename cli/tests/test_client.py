"""Unit tests for :class:`msgctl.client.MsgClient` against ``httpx.MockTransport``.

Covers bearer-header injection, retry-on-transient (5xx/429), no-retry-on-4xx,
problem+json → typed error mapping, and the security invariant that the raw token
never appears in an error message.
"""

from __future__ import annotations

import httpx
import pytest
from msgctl.client import AuthError, MsgClient, NotFoundError, RemoteError, TransientError

_TOKEN = "sk_secret_bearer_do_not_leak"


def _client(handler: httpx.MockTransport, *, token: str | None = _TOKEN) -> MsgClient:
    # Loopback base URL so the cleartext-http warning stays silent here; the
    # warning behavior itself is covered by the dedicated tests below.
    return MsgClient(
        "http://127.0.0.1:8000",
        token=token,
        transport=handler,
        backoff_base=0.0,  # instant retries in tests
        max_retries=4,
    )


def test_bearer_header_on_authed_call() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"streams": []})

    with _client(httpx.MockTransport(handler)) as c:
        c.get_sync()
    assert seen == [f"Bearer {_TOKEN}"]


def test_no_bearer_on_unauthed_call() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"token": "t", "user_id": "u_1"})

    with _client(httpx.MockTransport(handler), token=None) as c:
        c.setup(workspace_name="W", email="a@b.com", password="x" * 12, display_name="A")
    assert seen == [None]


def test_retry_on_5xx_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json={"accepted": [], "rejected": []})

    with _client(httpx.MockTransport(handler)) as c:
        resp = c.post_batch([])
    assert calls["n"] == 3
    assert resp == {"accepted": [], "rejected": []}


def test_retry_on_429() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, json={"detail": "slow down"})
        return httpx.Response(200, json={"streams": []})

    with _client(httpx.MockTransport(handler)) as c:
        c.get_sync()
    assert calls["n"] == 2


def test_transient_exhausted_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(TransientError):
            c.get_sync()


def test_no_retry_on_4xx() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"detail": "bad"})

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(RemoteError):
            c.get_sync()
    assert calls["n"] == 1  # permanent — exactly one attempt


def test_401_maps_to_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(AuthError):
            c.get_sync()


def test_404_maps_to_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(NotFoundError):
            c.get_events(stream_id="s_x", after=0, limit=500)


def test_token_never_in_error_message() -> None:
    """A 401 carrying the token in headers must not surface the token in the error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid"})

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(RemoteError) as exc_info:
            c.get_sync()
    assert _TOKEN not in str(exc_info.value)


def test_transport_error_retried_then_raised() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(TransientError):
            c.get_sync()
    assert calls["n"] == 5  # max_retries(4) + 1 initial attempt


def test_get_events_sends_query_params() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"events": [], "has_more": False})

    with _client(httpx.MockTransport(handler)) as c:
        c.get_events(stream_id="s_abc", after=7, limit=500)
    assert seen == {"stream_id": "s_abc", "after": "7", "limit": "500"}


# --- 422 field-error surfacing (ENG-107) ------------------------------------


def _raise_422(errors: object) -> str:
    """Drive a 422 with the given ``errors`` body and return the error message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": "one or more fields are invalid", "errors": errors}
        )

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(RemoteError) as exc_info:
            c.get_sync()
    return str(exc_info.value)


def test_422_surfaces_single_field_error() -> None:
    msg = _raise_422(
        [
            {
                "loc": ["body", "password"],
                "msg": "String should have at least 12 characters",
                "type": "string_too_short",
            }
        ]
    )
    assert msg == (
        "HTTP 422 on /v1/sync: one or more fields are invalid "
        "(password: String should have at least 12 characters)"
    )


def test_422_surfaces_multiple_field_errors_joined() -> None:
    msg = _raise_422(
        [
            {"loc": ["body", "password"], "msg": "too short", "type": "string_too_short"},
            {
                "loc": ["body", "email"],
                "msg": "must be a valid email address",
                "type": "value_error",
            },
        ]
    )
    assert "(password: too short; email: must be a valid email address)" in msg


def test_422_without_errors_array_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "one or more fields are invalid"})

    with _client(httpx.MockTransport(handler)) as c:
        with pytest.raises(RemoteError) as exc_info:
            c.get_sync()
    assert str(exc_info.value) == "HTTP 422 on /v1/sync: one or more fields are invalid"


def test_422_malformed_errors_entries_do_not_raise() -> None:
    # A grab-bag of malformed shapes: non-dict item, missing loc, empty loc,
    # missing msg, non-list loc. None must raise; usable ones still summarize.
    msg = _raise_422(
        [
            "not-a-dict",
            {"msg": "no loc here"},
            {"loc": [], "msg": "empty loc"},
            {"loc": ["body", "password"]},  # no msg -> skipped
            {"loc": "not-a-list", "msg": "weird loc"},
            {"loc": ["body", "workspace_name"], "msg": "usable"},
        ]
    )
    # It formed a valid message and surfaced the usable + best-effort entries.
    assert msg.startswith("HTTP 422 on /v1/sync: one or more fields are invalid (")
    assert "workspace_name: usable" in msg
    assert "?: no loc here" in msg  # missing loc -> "?" field name


def test_422_errors_are_capped_and_truncated() -> None:
    long_msg = "x" * 500
    msg = _raise_422([{"loc": ["body", f"f{i}"], "msg": long_msg, "type": "t"} for i in range(20)])
    # At most 5 field errors surfaced (defensive cap).
    assert msg.count("; ") == 4
    # Each long msg is truncated (the 500-char blob does not appear verbatim).
    assert long_msg not in msg
    assert "…" in msg


def test_422_field_errors_never_leak_submitted_value() -> None:
    # Even if a (non-conforming) server reintroduced input/ctx, the summary reads
    # only loc + msg, so the submitted secret never reaches the operator message.
    secret = "hunter2-super-secret"
    msg = _raise_422(
        [
            {
                "loc": ["body", "password"],
                "msg": "String should have at least 12 characters",
                "type": "string_too_short",
                "input": secret,
                "ctx": {"min_length": 12},
            }
        ]
    )
    assert secret not in msg
    assert "12 characters" in msg


# --- security: cleartext-http bearer-token warning --------------------------


def _noop_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={}))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://example.com:8000",
        "http://192.0.2.1",
    ],
)
def test_warns_on_cleartext_http_non_loopback(url: str, capsys: pytest.CaptureFixture[str]) -> None:
    MsgClient(url, token=_TOKEN, transport=_noop_transport()).close()
    err = capsys.readouterr().err
    assert "cleartext" in err
    assert "use https" in err
    assert _TOKEN not in err  # the warning names the host, never the token


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://example.com",
    ],
)
def test_no_cleartext_warning_for_loopback_or_https(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    MsgClient(url, token=_TOKEN, transport=_noop_transport()).close()
    assert "cleartext" not in capsys.readouterr().err
