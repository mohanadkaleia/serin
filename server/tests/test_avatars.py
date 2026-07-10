"""Profile pictures (ENG-152) — upload pipeline + the workspace-readable serve.

Adversarial where it matters, mirroring the ``test_files`` discipline:

* a valid upload stores the SERVER RE-ENCODE (normalized 256×256 WEBP, EXIF
  stripped, sha of the re-encode — never the raw bytes), sets
  ``users.avatar_sha256``, and emits EXACTLY ONE ``user.profile_updated``
  carrying the avatar ref;
* clear → NULL + one event carrying ``avatar_sha256: null``; idempotent;
* hostile input — a non-image, a lying ``image/*`` content type, an oversized
  body (declared AND chunked/undeclared), and a decompression bomb whose header
  claims 100000×100000 pixels — is rejected 400/413 BEFORE any giant decode,
  and nothing is stored / the row is untouched;
* the surface is STRUCTURALLY SELF-ONLY (no route targets another user's
  avatar for write);
* serve authz: any same-workspace principal (member and guest) can fetch any
  member's avatar; a cross-workspace caller and an unknown user get the SAME
  uniform 404 as "no avatar set" (no oracle); unauthenticated → 401; and the
  route is NOT a blob oracle — it takes a ``user_id`` (never a sha), so a
  known file-attachment sha cannot be fetched through it.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from authutil import (
    accept_invite,
    auth_header,
    create_invite,
    do_setup,
    fetch_meta_stream_id,
    fetch_stream_events,
    join_token,
)
from eventsutil import bootstrap_channel
from httpx import AsyncClient, Response
from msgd.auth.sessions import utcnow
from msgd.auth.tokens import hash_token
from msgd.blobs.store import LocalDiskBlobStore
from msgd.core import ids
from msgd.db.models import Device, Session, Stream, User, Workspace
from msgd.settings import Settings
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

AVATAR_URL = "/v1/me/avatar"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_store(settings: Settings) -> LocalDiskBlobStore:
    """A store rooted where the app writes its blobs (same as ``create_app``)."""
    return LocalDiskBlobStore(settings.data_dir / "blobs")


def _png_bytes(width: int, height: int, color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    """A real PNG of the given dimensions."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif(width: int = 320, height: int = 480) -> bytes:
    """A real JPEG carrying EXIF metadata (a camera-ish tag set) — the privacy
    payload the re-encode must strip."""
    img = Image.new("RGB", (width, height), (200, 30, 40))
    exif = Image.Exif()
    exif[0x010F] = "EvilCorp CameraPhone"  # Make
    exif[0x0110] = "Model X"  # Model
    exif[0x0131] = "leaky-editor/1.0"  # Software
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    data = buf.getvalue()
    # Sanity: the marker string is really in the raw upload.
    assert b"EvilCorp" in data
    return data


def _decompression_bomb_png() -> bytes:
    """A tiny PNG whose IHDR CLAIMS 100000×100000 pixels (10^10 declared pixels,
    a few dozen bytes on disk) — must be rejected pre-decode, never allocated."""

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0)
    return (
        sig
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + chunk(b"IEND", b"")
    )


async def _upload_avatar(
    client: AsyncClient, token: str, data: bytes, *, content_type: str = "image/png"
) -> Response:
    return await client.post(
        AVATAR_URL,
        content=data,
        headers={**auth_header(token), "Content-Type": content_type},
    )


async def _serve(client: AsyncClient, token: str, user_id: str) -> Response:
    return await client.get(f"/v1/users/{user_id}/avatar", headers=auth_header(token))


async def _seed_member(
    client: AsyncClient, owner_token: str, *, role: str = "member", email: str = "m@example.com"
) -> dict[str, Any]:
    inv = await create_invite(client, owner_token, role=role)
    raw = join_token(inv.json()["url"])
    body: dict[str, Any] = (await accept_invite(client, raw, email=email)).json()
    return body


def _problem_without_instance(resp: Response) -> dict[str, Any]:
    body = dict(resp.json())
    body.pop("instance", None)
    return body


async def _seed_workspace_b(db: AsyncSession) -> dict[str, str]:
    """Seed a second workspace + live session directly (setup is single-tenant).

    Mirrors ``test_files._seed_workspace``: the cross-workspace serve-authz test
    needs a caller whose ``workspace_id`` differs, which no API path can mint.
    """
    ws_id = ids.new_workspace_id()
    user_id = ids.new_user_id()
    device_id = ids.new_device_id()
    meta_id = ids.new_stream_id()
    raw_token = f"seed-token-{ws_id}"

    db.add(Workspace(workspace_id=ws_id, name="other"))
    await db.flush()
    db.add(
        User(
            user_id=user_id,
            workspace_id=ws_id,
            email="owner@other.example.com",
            password_hash="x",
            display_name="Other Owner",
            role="owner",
        )
    )
    await db.flush()
    db.add(Device(device_id=device_id, user_id=user_id))
    await db.flush()
    db.add(
        Session(
            token_hash=hash_token(raw_token),
            user_id=user_id,
            device_id=device_id,
            expires_at=utcnow() + timedelta(days=1),
        )
    )
    db.add(Stream(stream_id=meta_id, workspace_id=ws_id, kind="workspace-meta"))
    await db.flush()
    return {"workspace_id": ws_id, "user_id": user_id, "token": raw_token}


# --- upload: the safe re-encode pipeline ---------------------------------------


async def test_upload_stores_normalized_reencode_not_the_raw_bytes(
    client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """A valid upload → 200 with ``avatar_sha256`` set; the STORED blob is the
    server's 256×256 WEBP re-encode (raw bytes discarded), EXIF stripped."""
    owner = await do_setup(client)
    raw = _jpeg_with_exif(320, 480)  # non-square, EXIF-carrying

    resp = await _upload_avatar(client, owner["token"], raw, content_type="image/jpeg")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sha = body["avatar_sha256"]
    assert isinstance(sha, str) and len(sha) == 64
    # NOT the raw upload's digest — the stored object is the re-encode.
    assert sha != _sha(raw)

    stored = await _blob_store(settings).get_bytes(sha)
    assert _sha(stored) == sha  # content-addressed under its own digest
    assert stored != raw
    with Image.open(io.BytesIO(stored)) as img:
        assert img.format == "WEBP"
        assert img.size == (settings.avatar_px, settings.avatar_px)  # normalized square
        assert dict(img.getexif()) == {}  # EXIF/metadata stripped
    assert b"EvilCorp" not in stored

    # The row + a fresh GET /v1/me agree.
    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.avatar_sha256 == sha
    me = (await client.get("/v1/me", headers=auth_header(owner["token"]))).json()
    assert me["avatar_sha256"] == sha


async def test_upload_emits_exactly_one_profile_updated_with_avatar_ref(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await do_setup(client)
    meta_stream_id = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta_stream_id is not None
    before = await fetch_stream_events(db_session, meta_stream_id)

    resp = await _upload_avatar(client, owner["token"], _png_bytes(300, 300))
    assert resp.status_code == 200, resp.text
    sha = resp.json()["avatar_sha256"]

    after = await fetch_stream_events(db_session, meta_stream_id)
    assert len(after) == len(before) + 1  # exactly ONE appended event
    event = after[-1]
    assert event.type == "user.profile_updated"
    assert event.body["author_user_id"] == owner["user_id"]
    payload = event.body["payload"]
    assert payload["user_id"] == owner["user_id"]
    assert payload["avatar_sha256"] == sha
    # The event carries the RESULTING profile (display name intact).
    assert payload["display_name"] == "The Owner"


async def test_patch_me_after_upload_keeps_carrying_the_avatar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A later PATCH /v1/me event must carry the CURRENT avatar ref — a null
    there would fold as "avatar cleared" on every client (the resulting-values
    contract)."""
    owner = await do_setup(client)
    h = auth_header(owner["token"])
    up = await _upload_avatar(client, owner["token"], _png_bytes(64, 64))
    sha = up.json()["avatar_sha256"]
    meta_stream_id = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta_stream_id is not None

    resp = await client.patch("/v1/me", json={"display_name": "Renamed"}, headers=h)
    assert resp.status_code == 200
    assert resp.json()["avatar_sha256"] == sha  # PATCH echo keeps it

    event = (await fetch_stream_events(db_session, meta_stream_id))[-1]
    assert event.body["payload"]["display_name"] == "Renamed"
    assert event.body["payload"]["avatar_sha256"] == sha


async def test_clear_avatar_nulls_row_and_emits_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await do_setup(client)
    h = auth_header(owner["token"])
    assert (await _upload_avatar(client, owner["token"], _png_bytes(64, 64))).status_code == 200
    meta_stream_id = await fetch_meta_stream_id(db_session, owner["workspace_id"])
    assert meta_stream_id is not None
    before = await fetch_stream_events(db_session, meta_stream_id)

    resp = await client.delete(AVATAR_URL, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["avatar_sha256"] is None

    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.avatar_sha256 is None
    after = await fetch_stream_events(db_session, meta_stream_id)
    assert len(after) == len(before) + 1
    payload = after[-1].body["payload"]
    assert payload["user_id"] == owner["user_id"]
    assert payload["avatar_sha256"] is None

    # Clearing an already-absent avatar still succeeds (idempotent surface).
    again = await client.delete(AVATAR_URL, headers=h)
    assert again.status_code == 200
    assert again.json()["avatar_sha256"] is None


async def test_upload_requires_authentication(client: AsyncClient) -> None:
    assert (await client.post(AVATAR_URL, content=b"x")).status_code == 401
    assert (await client.delete(AVATAR_URL)).status_code == 401


# --- hostile input --------------------------------------------------------------


async def test_non_image_bytes_with_image_content_type_rejected_400(
    client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """The declared type LIES — the decode (magic bytes) is the real gate."""
    owner = await do_setup(client)
    data = b"definitely not a PNG despite the content type" * 10

    resp = await _upload_avatar(client, owner["token"], data, content_type="image/png")
    assert resp.status_code == 400, resp.text
    assert resp.json()["type"] == "/problems/invalid-image"

    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.avatar_sha256 is None
    # The raw bytes were never stored under their digest either.
    assert not await _blob_store(settings).exists(_sha(data))


async def test_non_image_content_type_rejected_400(client: AsyncClient) -> None:
    owner = await do_setup(client)
    resp = await _upload_avatar(
        client, owner["token"], _png_bytes(32, 32), content_type="text/plain"
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "/problems/invalid-image"


async def test_oversized_upload_rejected_413(
    client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Over ``avatar_max_size_bytes`` → 413 on BOTH the declared-length path and
    the chunked (no Content-Length) streaming path; nothing stored."""
    owner = await do_setup(client)
    too_big = b"\x89PNG\r\n\x1a\n" + b"\x00" * settings.avatar_max_size_bytes

    resp = await _upload_avatar(client, owner["token"], too_big)
    assert resp.status_code == 413, resp.text
    assert resp.json()["type"] == "/problems/file-too-large"

    async def _chunks() -> AsyncIterator[bytes]:
        # A chunked body carries no Content-Length — only the streaming cap
        # can stop it.
        for _ in range(6):
            yield b"\x00" * 1_048_576

    chunked = await client.post(
        AVATAR_URL,
        content=_chunks(),
        headers={**auth_header(owner["token"]), "Content-Type": "image/png"},
    )
    assert chunked.status_code == 413, chunked.text

    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.avatar_sha256 is None
    assert not await _blob_store(settings).exists(_sha(too_big))


async def test_decompression_bomb_rejected_400_before_decode(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A tiny PNG claiming 10^10 pixels → 400 via the PRE-DECODE pixel bound —
    the raster buffer is never allocated (no OOM)."""
    owner = await do_setup(client)
    bomb = _decompression_bomb_png()
    assert len(bomb) < 200  # tiny on the wire — the byte cap can't catch it

    resp = await _upload_avatar(client, owner["token"], bomb)
    assert resp.status_code == 400, resp.text
    assert resp.json()["type"] == "/problems/invalid-image"

    row = await db_session.get(User, owner["user_id"])
    assert row is not None and row.avatar_sha256 is None


# --- structurally self-only ------------------------------------------------------


async def test_no_route_writes_another_users_avatar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A member's upload changes ONLY the member; no write route takes a user_id."""
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])

    resp = await _upload_avatar(client, member["token"], _png_bytes(48, 48))
    assert resp.status_code == 200
    member_sha = resp.json()["avatar_sha256"]

    owner_row = await db_session.get(User, owner["user_id"])
    member_row = await db_session.get(User, member["user_id"])
    assert owner_row is not None and owner_row.avatar_sha256 is None
    assert member_row is not None and member_row.avatar_sha256 == member_sha

    # There is no per-user WRITE route to aim at another account.
    for method, url in (
        ("POST", f"/v1/users/{owner['user_id']}/avatar"),
        ("DELETE", f"/v1/users/{owner['user_id']}/avatar"),
        ("POST", f"/v1/me/avatar/{owner['user_id']}"),
    ):
        r = await client.request(
            method,
            url,
            content=_png_bytes(16, 16),
            headers={**auth_header(member["token"]), "Content-Type": "image/png"},
        )
        assert r.status_code in (404, 405), (method, url, r.status_code)
    owner_row = await db_session.get(User, owner["user_id"])
    assert owner_row is not None and owner_row.avatar_sha256 is None


# --- serve authz (the new read surface) -------------------------------------------


async def test_same_workspace_members_can_fetch_any_avatar(
    client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Member AND guest fetch the owner's avatar: 200, the re-encoded WEBP bytes,
    inline image/webp + nosniff + a content-addressed ETag."""
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])
    guest = await _seed_member(client, owner["token"], role="guest", email="g@example.com")
    up = await _upload_avatar(client, owner["token"], _png_bytes(500, 200))
    sha = up.json()["avatar_sha256"]
    stored = await _blob_store(settings).get_bytes(sha)

    for caller in (member, guest, owner):
        resp = await _serve(client, caller["token"], owner["user_id"])
        assert resp.status_code == 200, (caller["user_id"], resp.text)
        assert resp.content == stored  # exactly the server re-encode
        assert resp.headers["content-type"] == "image/webp"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["etag"] == f'"{sha}"'
        assert "private" in resp.headers["cache-control"]

    # Content-addressed revalidation: If-None-Match on the ETag → 304, no body.
    revalidated = await client.get(
        f"/v1/users/{owner['user_id']}/avatar",
        headers={**auth_header(member["token"]), "If-None-Match": f'"{sha}"'},
    )
    assert revalidated.status_code == 304
    assert revalidated.content == b""


async def test_serve_uniform_404_and_401(client: AsyncClient, db_session: AsyncSession) -> None:
    """Unknown user, no-avatar user, and cross-workspace user → the IDENTICAL
    404 body (no existence oracle); unauthenticated → 401."""
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])
    assert (await _upload_avatar(client, owner["token"], _png_bytes(64, 64))).status_code == 200
    other = await _seed_workspace_b(db_session)

    unknown = await _serve(client, member["token"], "u_00000000000000000000000000")
    no_avatar = await _serve(client, member["token"], member["user_id"])  # none set
    cross_ws = await _serve(client, other["token"], owner["user_id"])  # has one!

    assert unknown.status_code == no_avatar.status_code == cross_ws.status_code == 404
    assert (
        _problem_without_instance(unknown)
        == _problem_without_instance(no_avatar)
        == _problem_without_instance(cross_ws)
    )

    assert (await client.get(f"/v1/users/{owner['user_id']}/avatar")).status_code == 401


async def test_avatar_route_is_not_a_blob_oracle(
    client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Knowing a PRIVATE file attachment's sha256 gains nothing through the
    avatar surface: the route takes a user_id (never a sha), a sha-shaped
    "user_id" is a uniform 404, and there is no ``/v1/avatars/{sha}`` route.
    Only blobs some user row names in ``avatar_sha256`` are reachable."""
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])
    # A private channel only the OWNER is in, holding a secret attachment.
    private = await bootstrap_channel(
        client, db_session, owner, visibility="private", name="secret"
    )
    secret = b"private attachment bytes the member must never read" * 4
    secret_sha = _sha(secret)
    init = await client.post(
        "/v1/files/initiate",
        json={
            "sha256": secret_sha,
            "name": "secret.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": len(secret),
            "stream_id": private,
        },
        headers=auth_header(owner["token"]),
    )
    assert init.status_code == 200, init.text
    file_id = init.json()["file_id"]
    put = await client.put(
        f"/v1/files/{file_id}/blob", content=secret, headers=auth_header(owner["token"])
    )
    assert put.status_code == 200, put.text
    assert await _blob_store(settings).exists(secret_sha)  # the blob IS on disk

    # The attachment sha is unreachable through every avatar-shaped path.
    for url in (
        f"/v1/users/{secret_sha}/avatar",  # sha smuggled as a "user_id"
        f"/v1/avatars/{secret_sha}",  # no by-hash route exists
        f"/v1/users/{owner['user_id']}/avatar/{secret_sha}",
    ):
        resp = await client.get(url, headers=auth_header(member["token"]))
        assert resp.status_code == 404, (url, resp.status_code)
        assert secret not in resp.content

    # And the owner's REAL avatar route serves only their avatar re-encode —
    # never an attachment — because resolution is user_id → avatar_sha256.
    up = await _upload_avatar(client, owner["token"], _png_bytes(64, 64))
    served = await _serve(client, member["token"], owner["user_id"])
    assert served.status_code == 200
    assert _sha(served.content) == up.json()["avatar_sha256"] != secret_sha
    assert served.content != secret


async def test_deactivated_users_avatar_still_serves(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """History still renders: a deactivated member's avatar stays fetchable by
    the workspace (their messages remain visible, so their face does too)."""
    owner = await do_setup(client)
    member = await _seed_member(client, owner["token"])
    up = await _upload_avatar(client, member["token"], _png_bytes(64, 64))
    assert up.status_code == 200

    deact = await client.patch(
        f"/v1/admin/members/{member['user_id']}",
        json={"active": False},
        headers=auth_header(owner["token"]),
    )
    assert deact.status_code == 200, deact.text

    resp = await _serve(client, owner["token"], member["user_id"])
    assert resp.status_code == 200
