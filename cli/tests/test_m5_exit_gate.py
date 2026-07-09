"""M5 EXIT GATE (ENG-163): GitHub PR events into the workspace via the PUBLIC
plugin API only.

This is the M5 analogue of the M4 ENG-158 round-trip gate, on the same
real-stack mechanism (Postgres testcontainer + subprocess uvicorn —
``_e2e_server``). It proves the §13 M5 exit criterion end to end: the
**reference plugin** (``plugins/github_notifier``, booted as a REAL
``python -m github_notifier`` subprocess) turns recorded GitHub webhook
deliveries into channel messages using nothing but surfaces a plugin author
can reach — no test-only seams, no direct DB writes on the happy path.

The provision → deliver → observe pipeline:

1. Boot the server; ``/v1/setup`` the owner; invite a second HUMAN member
   (the live-WS witness).
2. Provision via the public plugin API only: ``POST /v1/plugins/bots``
   (``github-bot``, install scope ``events:write``, granted ``#general``) and
   ``POST /v1/plugins/hooks`` bound to ``#general`` with that bot as author —
   capturing the capability URL, the ONE time it ever exists.
3. Boot the notifier subprocess with ``GITHUB_WEBHOOK_SECRET`` +
   ``MSG_HOOK_URL=<capability URL>``; wait on its ``GET /healthz``.
4. Replay the recorded ``pull_request`` ``opened`` + ``closed``(merged)
   fixtures (``plugins/github_notifier/testdata/``) against the plugin with a
   VALID ``X-Hub-Signature-256`` (the plugin's own ``sign()`` over the raw
   fixture bytes) and real ``X-GitHub-Event`` / ``X-GitHub-Delivery`` headers.
5. Observe via the MEMBER API: ``GET /v1/events`` shows exactly the two
   ``message.created`` events, authored by the bot's ``(user, device)``,
   ``format="plain"`` / ``mentions=[]`` (the ENG-161 injection guard), text
   carrying the fixtures' PR number/title/URL; ``/v1/admin/members`` shows the
   author ``is_bot=true`` (role ``guest``); and a second member's WebSocket —
   opened BEFORE the replay — received both frames LIVE.

The four adversary legs (§12 invariant-8 "plugin containment" — each asserts
ZERO effect, and they are permanent CI checks by living here):

a. A TAMPERED ``X-Hub-Signature-256`` → the plugin drops the delivery (401)
   and nothing is forwarded — no new event.
b. A direct ``POST /v1/hooks/<wrong-token>`` → the uniform 404 — no new event.
c. The bot's own minted token uploading a ``message.created`` into a stream it
   was NEVER granted → ``permission_denied`` (containment is membership, not
   the verb scope).
d. A normal member uploading a forged ``bot.installed`` →
   ``permission_denied`` (the SERVER_AUTHORED guard) — a client credential can
   never manufacture plugin-era meta state.

Deterministic + bounded: the plugin answers its 200 only AFTER the hook POST
succeeded, so the events are queryable the moment the replay returns — the
only polling anywhere is the two readiness probes and the WS reads (bounded).

Marked ``integration`` (needs Docker); ``-m "not integration"`` skips it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from _e2e_server import ServerHandle, _free_port, _wait_healthy, start_live_server
from github_notifier.signature import sign
from msgd.core import ids
from msgd.core.hashing import hash_event
from msgd.core.payloads import build_message_created_body
from msgd.core.time import now_rfc3339
from websockets.typing import Subprotocol

pytestmark = pytest.mark.integration

OWNER_PASSWORD = "correct-horse-battery-staple"
MEMBER_PASSWORD = "another-valid-password-42"

#: The GitHub webhook shared secret for this run — known to the test (which
#: plays GitHub and signs deliveries) and to the plugin subprocess (its env).
WEBHOOK_SECRET = "m5-exit-gate-webhook-secret"

TESTDATA = Path(__file__).resolve().parents[2] / "plugins" / "github_notifier" / "testdata"

#: What ``format_pull_request`` renders for the two replayed fixtures — asserted
#: exactly, so a formatting regression (lost PR number/title/URL) fails the gate.
EXPECTED_OPENED = (
    "PR #42 opened by alice: Add rate limiting to the sync endpoint"
    " — https://github.com/example-org/example-repo/pull/42"
)
EXPECTED_MERGED = (
    "PR #41 merged by bob: Fix flaky WebSocket heartbeat test"
    " — https://github.com/example-org/example-repo/pull/41"
)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServerHandle]:
    """The one live msg instance this gate drives."""
    with start_live_server(tmp_path_factory) as handle:
        yield handle


def _hdr(auth: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['token']}"}


def _accept_invite(
    client: httpx.Client,
    owner: dict[str, Any],
    *,
    role: str,
    email: str,
    display_name: str,
    password: str,
) -> dict[str, Any]:
    inv = client.post("/v1/admin/invites", json={"role": role}, headers=_hdr(owner))
    assert inv.status_code == 201, inv.text
    raw_token = inv.json()["url"].rsplit("/join/", 1)[1]
    joined = client.post(
        "/v1/auth/accept-invite",
        json={
            "token": raw_token,
            "email": email,
            "display_name": display_name,
            "password": password,
        },
    )
    assert joined.status_code == 200, joined.text
    auth: dict[str, Any] = joined.json()
    return auth


def _body(
    auth: dict[str, Any], stream_id: str, type_: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "event_id": ids.new_event_id(),
        "workspace_id": auth["workspace_id"],
        "stream_id": stream_id,
        "type": type_,
        "type_version": 1,
        "author_user_id": auth["user_id"],
        "author_device_id": auth["device_id"],
        "client_created_at": now_rfc3339(),
        "payload": payload,
    }


def _stream_bodies(
    client: httpx.Client, auth: dict[str, Any], stream_id: str
) -> list[dict[str, Any]]:
    """Every event BODY in ``stream_id``, ascending — the member-visible log."""
    resp = client.get("/v1/events", params={"stream_id": stream_id}, headers=_hdr(auth))
    assert resp.status_code == 200, resp.text
    return [e["body"] for e in resp.json()["events"]]


def _github_headers(
    raw_body: bytes, *, delivery_id: str, signature: str | None = None
) -> dict[str, str]:
    """The three headers GitHub sends with a webhook delivery (valid sig default)."""
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": (
            signature if signature is not None else sign(WEBHOOK_SECRET.encode("utf-8"), raw_body)
        ),
    }


@contextmanager
def _run_notifier(hook_url: str, log_path: Path) -> Iterator[str]:
    """Boot ``python -m github_notifier`` (the SAME interpreter/venv as this
    test) against ``hook_url``; yield its base URL once ``/healthz`` answers;
    always terminate + reap on exit."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "MSG_HOOK_URL": hook_url,
        "GITHUB_NOTIFIER_HOST": "127.0.0.1",
        "GITHUB_NOTIFIER_PORT": str(port),
    }
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "github_notifier"],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_healthy(base_url)
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def _bearer(token: str) -> list[Subprotocol]:
    return [Subprotocol("bearer"), Subprotocol(token)]


async def _ws_read_until(ws: Any, t: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Receive frames until one with ``{"t": t}`` arrives (skips heartbeat noise)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.01))
        msg = json.loads(raw)
        if isinstance(msg, dict) and msg.get("t") == t:
            return msg


async def _ws_read_texts(ws: Any, want: set[str], *, timeout: float = 10.0) -> None:
    """Receive frames until every text in ``want`` arrived as a live event frame."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    remaining_texts = set(want)
    while remaining_texts:
        budget = deadline - loop.time()
        frame = await asyncio.wait_for(_ws_read_until(ws, "event"), timeout=max(budget, 0.01))
        body = frame["event"]["body"]
        if body["type"] == "message.created":
            remaining_texts.discard(body["payload"]["text"])


async def test_m5_github_events_via_public_plugin_api(server: ServerHandle, tmp_path: Path) -> None:
    base_url = server.base_url

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        # ==== Step 1: owner + #general + a second human member ================
        resp = client.post(
            "/v1/setup",
            json={
                "workspace_name": "Acme",
                "email": "owner@example.com",
                "password": OWNER_PASSWORD,
                "display_name": "Owner",
            },
        )
        assert resp.status_code == 200, resp.text
        owner: dict[str, Any] = resp.json()
        member = _accept_invite(
            client,
            owner,
            role="member",
            email="bob@example.com",
            display_name="Bob",
            password=MEMBER_PASSWORD,
        )

        sync = client.get("/v1/sync", headers=_hdr(owner)).json()
        general_id = next(s["stream_id"] for s in sync["streams"] if s.get("name") == "general")
        meta_id = next(s["stream_id"] for s in sync["streams"] if s["kind"] == "workspace-meta")

        # ==== Step 2: provision via the PUBLIC plugin API only ================
        created = client.post(
            "/v1/plugins/bots",
            json={"name": "github-bot", "scopes": ["events:write"], "stream_ids": [general_id]},
            headers=_hdr(owner),
        )
        assert created.status_code == 201, created.text
        bot = created.json()
        assert bot["role"] == "guest"
        assert bot["stream_ids"] == [general_id]

        hook = client.post(
            "/v1/plugins/hooks",
            json={"stream_id": general_id, "bot_user_id": bot["bot_user_id"], "name": "github"},
            headers=_hdr(owner),
        )
        assert hook.status_code == 201, hook.text
        hook_url: str = hook.json()["url"]
        assert "/v1/hooks/" in hook_url
        assert hook.json()["bot_user_id"] == bot["bot_user_id"]

        # A scoped bot token — the credential adversary leg (c) drives with it.
        minted = client.post(
            f"/v1/plugins/bots/{bot['bot_user_id']}/tokens", json={}, headers=_hdr(owner)
        )
        assert minted.status_code == 201, minted.text
        bot_token: str = minted.json()["token"]
        assert minted.json()["scopes"] == ["events:write"]  # inherited install scopes

        # A channel the bot was NEVER granted (private, like the M4 gate's) —
        # the containment boundary leg (c) targets it.
        ungranted_id = ids.new_stream_id()
        ungranted_body = _body(
            owner,
            ungranted_id,
            "channel.created",
            {"channel_stream_id": ungranted_id, "name": "secret-plans", "visibility": "private"},
        )
        resp = client.post(
            "/v1/events/batch",
            json={"events": [{"body": ungranted_body, "event_hash": hash_event(ungranted_body)}]},
            headers=_hdr(owner),
        )
        assert resp.status_code == 200 and resp.json()["rejected"] == [], resp.text

        # ==== Steps 3+4+5: plugin subprocess + live WS witness + replay =======
        ws_url = base_url.replace("http://", "ws://", 1) + "/v1/ws"
        with _run_notifier(hook_url, tmp_path / "github_notifier.log") as plugin_url:
            # The member's WS opens BEFORE the replay; the ping/pong barrier
            # proves hub registration, so the fanout cannot race past it.
            async with websockets.connect(
                ws_url, subprotocols=_bearer(member["token"]), open_timeout=10
            ) as ws:
                assert ws.subprotocol == "bearer"
                await ws.send(json.dumps({"t": "ping"}))
                await _ws_read_until(ws, "pong")

                # Replay the recorded fixtures with VALID signatures — this is
                # GitHub's exact wire shape, addressed to the plugin.
                for fixture, delivery in (
                    ("pull_request.opened.json", "m5-delivery-opened"),
                    ("pull_request.closed_merged.json", "m5-delivery-merged"),
                ):
                    raw = (TESTDATA / fixture).read_bytes()
                    answer = httpx.post(
                        f"{plugin_url}/webhook",
                        content=raw,
                        headers=_github_headers(raw, delivery_id=delivery),
                        timeout=30.0,
                    )
                    assert answer.status_code == 200, (fixture, answer.text)
                    assert answer.json() == {"ok": True}

                # Live proof: the second member received both frames over the
                # WS that was open before the first delivery.
                await _ws_read_texts(ws, {EXPECTED_OPENED, EXPECTED_MERGED})

            # ==== Step 5 (cont.): observe via the member API ===================
            bodies = _stream_bodies(client, owner, general_id)
            messages = [b for b in bodies if b["type"] == "message.created"]
            assert bodies == messages  # #general's log is exactly the two posts
            assert [m["payload"]["text"] for m in messages] == [EXPECTED_OPENED, EXPECTED_MERGED]
            for m in messages:
                # Authored as the bot's (user, device); the ENG-161 injection
                # guard shape: plain format, no mentions/files/thread.
                assert m["author_user_id"] == bot["bot_user_id"]
                assert m["author_device_id"] == bot["device_id"]
                assert m["payload"]["format"] == "plain"
                assert m["payload"]["mentions"] == []
                assert m["payload"]["file_ids"] == []
                assert m["payload"]["thread_root_id"] is None
            # The member (not only the owner/admin) reads the same two messages.
            assert _stream_bodies(client, member, general_id) == bodies

            roster = client.get("/v1/admin/members", headers=_hdr(owner))
            assert roster.status_code == 200, roster.text
            author = next(u for u in roster.json()["members"] if u["user_id"] == bot["bot_user_id"])
            assert author["is_bot"] is True
            assert author["role"] == "guest"
            assert author["deactivated"] is False

            baseline = _stream_bodies(client, owner, general_id)

            # ==== Adversary leg (a): tampered X-Hub-Signature-256 ==============
            raw = (TESTDATA / "pull_request.review_requested.json").read_bytes()
            forged_sig = sign(b"the-wrong-secret", raw)
            tampered = _github_headers(
                raw, delivery_id="m5-delivery-tampered", signature=forged_sig
            )
            answer = httpx.post(
                f"{plugin_url}/webhook", content=raw, headers=tampered, timeout=30.0
            )
            assert answer.status_code == 401, answer.text  # dropped before parsing
            assert _stream_bodies(client, owner, general_id) == baseline

        # ==== Adversary leg (b): a guessed capability URL ======================
        wrong = client.post(f"/v1/hooks/{'f' * 43}", json={"text": "forged delivery"})
        assert wrong.status_code == 404, wrong.text  # the uniform not_found
        assert _stream_bodies(client, owner, general_id) == baseline

        # ==== Adversary leg (c): the bot token outside its granted streams ====
        escape = build_message_created_body(
            workspace_id=owner["workspace_id"],
            stream_id=ungranted_id,
            author_user_id=bot["bot_user_id"],
            author_device_id=bot["device_id"],
            client_created_at=now_rfc3339(),
            text="bot escaping its grants",
        ).model_dump(mode="json")
        resp = client.post(
            "/v1/events/batch",
            json={"events": [{"body": escape, "event_hash": hash_event(escape)}]},
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["accepted"] == []
        assert resp.json()["rejected"][0]["code"] == "permission_denied"
        # Zero effect: the private channel's log is still just its genesis event.
        private_bodies = _stream_bodies(client, owner, ungranted_id)
        assert [b["type"] for b in private_bodies] == ["channel.created"]

        # ==== Adversary leg (d): a member forging bot.installed ================
        forged = _body(
            member,
            meta_id,
            "bot.installed",
            {"bot_user_id": ids.new_user_id(), "name": "evil-bot", "scopes": ["events:write"]},
        )
        resp = client.post(
            "/v1/events/batch",
            json={"events": [{"body": forged, "event_hash": hash_event(forged)}]},
            headers=_hdr(member),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["accepted"] == []
        rejected = resp.json()["rejected"][0]
        assert rejected["code"] == "permission_denied"
        assert rejected["detail"] == "event type is server-authored and cannot be uploaded"
        # Zero effect: the meta log carries exactly ONE bot.installed — the real one.
        meta_bodies = _stream_bodies(client, owner, meta_id)
        installed = [b for b in meta_bodies if b["type"] == "bot.installed"]
        assert [b["payload"]["bot_user_id"] for b in installed] == [bot["bot_user_id"]]

        # The happy-path log never moved during the adversary phase.
        assert _stream_bodies(client, owner, general_id) == baseline
