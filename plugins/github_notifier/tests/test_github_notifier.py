"""GitHub notifier behavior tests (ENG-162) — no network, fixtures only.

Everything runs against the transport-free :class:`Notifier` pipeline with an
injected recording ``post``, plus one loopback end-to-end test that boots the
real stdlib HTTP server and a mock msg hook. CI never touches github.com (the
``pull_request`` payloads are the hand-authored fixtures in ``testdata/``) and
never touches a real msg server (the hook is a local mock).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from github_notifier.config import (
    ENV_HOOK_URL,
    ENV_PORT,
    ENV_SECRET,
    Config,
    ConfigError,
    load_config,
)
from github_notifier.dedupe import DeliveryLog
from github_notifier.notifier import Notifier, Response
from github_notifier.server import build_server
from github_notifier.signature import sign, verify_signature

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
SECRET = b"test-webhook-secret"
HOOK_URL = "http://msg.invalid/v1/hooks/hook-token-for-tests"


def fixture_bytes(name: str) -> bytes:
    return (TESTDATA / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    data = json.loads(fixture_bytes(name))
    assert isinstance(data, dict)
    return data


class RecordingPost:
    """A fake outbound-post seam: records ``(url, text)`` calls, returns ``result``."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, text: str) -> bool:
        self.calls.append((url, text))
        return self.result


def make_notifier(post: RecordingPost) -> Notifier:
    config = Config(webhook_secret=SECRET, hook_url=HOOK_URL, host="127.0.0.1", port=0)
    return Notifier(config, post=post)


def deliver(
    notifier: Notifier,
    body: bytes,
    *,
    event: str | None = "pull_request",
    delivery_id: str | None = "delivery-1",
    signature: str | None = None,
    secret: bytes = SECRET,
) -> Response:
    """Send one delivery through the pipeline; default = a correctly signed one."""
    if signature is None:
        signature = sign(secret, body)
    return notifier.handle(event=event, delivery_id=delivery_id, signature=signature, raw_body=body)


# --- formatting: fixture payload -> the expected {"text": ...} POST ---------------


def test_opened_posts_formatted_text() -> None:
    post = RecordingPost()
    response = deliver(make_notifier(post), fixture_bytes("pull_request.opened.json"))
    assert response.status == 200
    assert response.body == {"ok": True}
    assert len(post.calls) == 1
    url, text = post.calls[0]
    assert url == HOOK_URL
    assert text == (
        "PR #42 opened by alice: Add rate limiting to the sync endpoint"
        " — https://github.com/example-org/example-repo/pull/42"
    )


def test_closed_merged_posts_merged_text() -> None:
    post = RecordingPost()
    response = deliver(make_notifier(post), fixture_bytes("pull_request.closed_merged.json"))
    assert response.status == 200
    assert len(post.calls) == 1
    _, text = post.calls[0]
    assert "PR #41 merged by bob" in text
    assert "Fix flaky WebSocket heartbeat test" in text
    assert "https://github.com/example-org/example-repo/pull/41" in text


def test_closed_unmerged_posts_closed_text() -> None:
    payload = fixture_json("pull_request.closed_merged.json")
    payload["pull_request"]["merged"] = False
    payload["pull_request"]["merged_at"] = None
    payload["pull_request"]["merged_by"] = None
    body = json.dumps(payload).encode()
    post = RecordingPost()
    response = deliver(make_notifier(post), body)
    assert response.status == 200
    assert len(post.calls) == 1
    assert post.calls[0][1].startswith("PR #41 closed by bob:")


def test_review_requested_posts_reviewer_text() -> None:
    post = RecordingPost()
    response = deliver(make_notifier(post), fixture_bytes("pull_request.review_requested.json"))
    assert response.status == 200
    assert len(post.calls) == 1
    _, text = post.calls[0]
    assert text == (
        "PR #43 review requested from carol by alice: Extract the blob store behind"
        " an interface — https://github.com/example-org/example-repo/pull/43"
    )


# --- signature verification: the security gate -------------------------------------


def test_garbage_signature_header_is_dropped_without_post() -> None:
    post = RecordingPost()
    response = deliver(
        make_notifier(post), fixture_bytes("pull_request.opened.json"), signature="garbage"
    )
    assert response.status == 401
    assert post.calls == []


def test_absent_signature_header_is_dropped_without_post() -> None:
    post = RecordingPost()
    body = fixture_bytes("pull_request.opened.json")
    response = make_notifier(post).handle(
        event="pull_request", delivery_id="d-abs", signature=None, raw_body=body
    )
    assert response.status == 401
    assert post.calls == []


def test_tampered_body_is_dropped_without_post() -> None:
    post = RecordingPost()
    body = fixture_bytes("pull_request.opened.json")
    good_signature = sign(SECRET, body)
    tampered = body.replace(b"alice", b"mallory")
    response = make_notifier(post).handle(
        event="pull_request", delivery_id="d-tam", signature=good_signature, raw_body=tampered
    )
    assert response.status == 401
    assert post.calls == []


def test_wrong_secret_is_dropped_without_post() -> None:
    post = RecordingPost()
    response = deliver(
        make_notifier(post), fixture_bytes("pull_request.opened.json"), secret=b"not-the-secret"
    )
    assert response.status == 401
    assert post.calls == []


def test_verify_signature_units() -> None:
    body = b'{"zen": "Keep it logically awesome."}'
    assert verify_signature(SECRET, body, sign(SECRET, body))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha256=" + "0" * 64)
    assert not verify_signature(SECRET, body, "sha1=whatever")
    # Non-ASCII header must be a clean False, not a TypeError from compare_digest.
    assert not verify_signature(SECRET, body, "sha256=café")


# --- dedupe: GitHub redelivers; the msg hook has no idempotency key ---------------


def test_duplicate_delivery_id_posts_once() -> None:
    post = RecordingPost()
    notifier = make_notifier(post)
    body = fixture_bytes("pull_request.opened.json")
    first = deliver(notifier, body, delivery_id="dup-1")
    second = deliver(notifier, body, delivery_id="dup-1")
    assert first.status == 200
    assert second.status == 200
    assert second.body.get("ignored") == "duplicate delivery"
    assert len(post.calls) == 1


def test_failed_forward_is_not_recorded_so_redelivery_retries() -> None:
    post = RecordingPost(result=False)
    notifier = make_notifier(post)
    body = fixture_bytes("pull_request.opened.json")
    failed = deliver(notifier, body, delivery_id="retry-1")
    assert failed.status == 502  # GitHub marks it failed and can redeliver
    post.result = True
    retried = deliver(notifier, body, delivery_id="retry-1")
    assert retried.status == 200
    assert len(post.calls) == 2  # the failed attempt + the successful redelivery


def test_delivery_log_is_bounded_lru() -> None:
    log = DeliveryLog(capacity=2)
    log.add("a")
    log.add("b")
    assert "a" in log  # refreshes "a"
    log.add("c")  # evicts "b" (least recently seen)
    assert "b" not in log
    assert "a" in log and "c" in log
    assert len(log) == 2
    with pytest.raises(ValueError):
        DeliveryLog(capacity=0)


# --- ignored events/actions: 200, no outbound POST --------------------------------


def test_non_pull_request_event_is_acked_without_post() -> None:
    post = RecordingPost()
    body = json.dumps({"zen": "Anything added dilutes everything else."}).encode()
    response = deliver(make_notifier(post), body, event="push", delivery_id="push-1")
    assert response.status == 200
    assert response.body["ok"] is True
    assert post.calls == []


def test_unhandled_action_is_acked_without_post() -> None:
    payload = fixture_json("pull_request.opened.json")
    payload["action"] = "synchronize"
    post = RecordingPost()
    response = deliver(make_notifier(post), json.dumps(payload).encode(), delivery_id="sync-1")
    assert response.status == 200
    assert response.body.get("ignored") == "unhandled action"
    assert post.calls == []


def test_signed_garbage_json_is_rejected_without_post() -> None:
    post = RecordingPost()
    response = deliver(make_notifier(post), b"not json at all", delivery_id="garbage-1")
    assert response.status == 400
    assert post.calls == []


# --- config: fail fast, naming the variable ----------------------------------------


def test_load_config_requires_secret_and_hook_url() -> None:
    with pytest.raises(ConfigError, match=ENV_SECRET):
        load_config({})
    with pytest.raises(ConfigError, match=ENV_HOOK_URL):
        load_config({ENV_SECRET: "s3cr3t"})
    with pytest.raises(ConfigError, match="http"):
        load_config({ENV_SECRET: "s3cr3t", ENV_HOOK_URL: "ftp://nope"})
    with pytest.raises(ConfigError, match=ENV_PORT):
        load_config({ENV_SECRET: "s3cr3t", ENV_HOOK_URL: "http://h/v1/hooks/t", ENV_PORT: "x"})


def test_load_config_defaults_and_overrides() -> None:
    config = load_config({ENV_SECRET: "s3cr3t", ENV_HOOK_URL: "https://msg.example/v1/hooks/t"})
    assert config.webhook_secret == b"s3cr3t"
    assert config.hook_url == "https://msg.example/v1/hooks/t"
    assert (config.host, config.port) == ("127.0.0.1", 8477)
    overridden = load_config(
        {
            ENV_SECRET: "s3cr3t",
            ENV_HOOK_URL: "https://msg.example/v1/hooks/t",
            "GITHUB_NOTIFIER_HOST": "0.0.0.0",
            ENV_PORT: "0",
        }
    )
    assert (overridden.host, overridden.port) == ("0.0.0.0", 0)


# --- end-to-end over loopback HTTP: real server, real signature, mock msg hook -----


class _MockHook:
    """A loopback stand-in for msg's ``POST /v1/hooks/{token}`` receiver."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
                length = int(self.headers.get("Content-Length") or "0")
                received.append(json.loads(self.rfile.read(length)))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/hooks/mock-token"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_end_to_end_over_http() -> None:
    """Boot the real notifier server; a signed fixture arrives at the mock hook."""
    hook = _MockHook()
    config = Config(webhook_secret=SECRET, hook_url=hook.url, host="127.0.0.1", port=0)
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}/"
    body = fixture_bytes("pull_request.opened.json")
    try:
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "e2e-delivery-1",
                "X-Hub-Signature-256": sign(SECRET, body),
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"ok": True}
        assert hook.received == [
            {
                "text": "PR #42 opened by alice: Add rate limiting to the sync endpoint"
                " — https://github.com/example-org/example-repo/pull/42"
            }
        ]

        # A tampered delivery over the same real socket: 401, nothing forwarded.
        tampered = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "e2e-delivery-2",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(tampered, timeout=5)
        assert excinfo.value.code == 401
        assert len(hook.received) == 1

        # The health probe the M5 exit gate uses to detect readiness.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as health:
            assert json.loads(health.read()) == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        hook.close()
