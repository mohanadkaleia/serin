"""``GET /v1/search`` — FTS relevance, THE permission-isolation crux, filters, pagination (ENG-122).

Principals are minted through the real auth path (setup + invite/accept, so each
carries a bearer token); streams + memberships are seeded at the DB layer and
messages through the real :func:`~msgd.events.insert.insert_event` (so the STORED
GENERATED ``search_tsv`` is computed by Postgres exactly as production does).
Requests run through the in-process ``client`` sharing the rolled-back session.

The load-bearing file is the permission-isolation set: a term that lives ONLY in
a stream the caller cannot read returns ZERO hits, proven non-vacuous by a reader
who CAN see the stream getting the hit for the identical query.
"""

from __future__ import annotations

from typing import Any

from authutil import (
    accept_invite,
    auth_header,
    create_invite,
    do_setup,
    fetch_meta_stream_id,
    join_token,
)
from httpx import AsyncClient
from msgd.core import ids
from msgd.core.payloads import build_message_created_body
from msgd.core.time import now_rfc3339
from msgd.db.models import Stream, StreamMember
from msgd.events.insert import insert_event
from sqlalchemy.ext.asyncio import AsyncSession


async def _invited_user(
    client: AsyncClient, owner_token: str, *, role: str, email: str
) -> dict[str, Any]:
    """Create + accept an invite; return the new principal's login body."""
    inv = await create_invite(client, owner_token, role=role)
    raw = join_token(inv.json()["url"])
    accepted = await accept_invite(client, raw, email=email)
    assert accepted.status_code == 200, accepted.text
    body: dict[str, Any] = accepted.json()
    return body


def _add_stream(
    db: AsyncSession,
    *,
    ws: str,
    kind: str = "channel",
    name: str | None = None,
    visibility: str | None = None,
) -> str:
    sid = ids.new_stream_id()
    db.add(Stream(stream_id=sid, workspace_id=ws, kind=kind, name=name, visibility=visibility))
    return sid


async def _seed_message(
    db: AsyncSession,
    *,
    ws: str,
    uid: str,
    did: str,
    sid: str,
    text: str,
) -> str:
    """Insert a real ``message.created`` (projection + generated ``search_tsv``); return its id."""
    mid = ids.new_message_id()
    body: dict[str, Any] = build_message_created_body(
        workspace_id=ws,
        stream_id=sid,
        author_user_id=uid,
        author_device_id=did,
        client_created_at=now_rfc3339(),
        text=text,
        message_id=mid,
    ).model_dump(mode="json")
    await insert_event(db, stream_id=sid, body=body)
    await db.flush()
    return mid


async def _delete_message(
    db: AsyncSession, *, ws: str, uid: str, did: str, sid: str, message_id: str
) -> None:
    """Insert a real ``message.deleted`` tombstone (redacts text to '' in the projection)."""
    body: dict[str, Any] = {
        "event_id": ids.new_event_id(),
        "workspace_id": ws,
        "stream_id": sid,
        "type": "message.deleted",
        "type_version": 1,
        "author_user_id": uid,
        "author_device_id": did,
        "client_created_at": now_rfc3339(),
        "payload": {"message_id": message_id},
    }
    await insert_event(db, stream_id=sid, body=body)
    await db.flush()


async def _search(client: AsyncClient, token: str, **params: Any) -> Any:
    """GET /v1/search with the given query params (None values dropped); return the response."""
    query = {k: v for k, v in params.items() if v is not None}
    return await client.get("/v1/search", params=query, headers=auth_header(token))


async def _hits(client: AsyncClient, token: str, **params: Any) -> list[dict[str, Any]]:
    resp = await _search(client, token, **params)
    assert resp.status_code == 200, resp.text
    hits: list[dict[str, Any]] = resp.json()["hits"]
    return hits


async def _member_public_channel(db: AsyncSession, *, ws: str, uid: str, name: str) -> str:
    """A public channel with ``uid`` as an explicit member (readable by non-guests anyway)."""
    sid = _add_stream(db, ws=ws, name=name, visibility="public")
    await db.flush()
    db.add(StreamMember(stream_id=sid, user_id=uid))
    await db.flush()
    return sid


# --- relevance / ranking + websearch semantics --------------------------------


async def test_relevance_ranking_and_websearch_semantics(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A matching readable message is returned; more-relevant outranks less-relevant."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    sid = await _member_public_channel(db_session, ws=ws, uid=uid, name="general")

    strong = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="banana banana banana smoothie"
    )
    weak = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="a single banana bread recipe"
    )
    await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=sid, text="totally unrelated text")

    hits = await _hits(client, o["token"], q="banana")
    by_id = {h["message_id"]: h for h in hits}
    assert set(by_id) == {strong, weak}  # only the two banana messages, unrelated excluded
    # ts_rank_cd: the message with more/denser occurrences scores strictly higher.
    assert by_id[strong]["rank"] > by_id[weak]["rank"]


async def test_websearch_phrase_or_negation(client: AsyncClient, db_session: AsyncSession) -> None:
    """``websearch_to_tsquery`` phrase / OR / negation semantics behave."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    sid = await _member_public_channel(db_session, ws=ws, uid=uid, name="general")

    fox = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="the quick brown fox jumps"
    )
    reversed_ = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="a brown and quick hare"
    )
    apple = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="fresh apple pie"
    )
    orange = await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=sid, text="orange juice")

    # Phrase: "quick brown" matches the adjacent-order message only.
    phrase = {h["message_id"] for h in await _hits(client, o["token"], q='"quick brown"')}
    assert phrase == {fox}
    assert reversed_ not in phrase

    # OR: apple OR orange matches both fruit messages.
    either = {h["message_id"] for h in await _hits(client, o["token"], q="apple OR orange")}
    assert either == {apple, orange}

    # Negation: quick -hare excludes the hare message, keeps the fox.
    negated = {h["message_id"] for h in await _hits(client, o["token"], q="quick -hare")}
    assert negated == {fox}


# --- THE CRUX: permission isolation -------------------------------------------


async def test_isolation_private_stream_zero_hits(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A non-member searching a term that lives ONLY in a private stream gets ZERO hits.

    Non-vacuous: the owner (a member of the private stream) gets the hit for the
    identical query — so the term IS indexed and searchable; the adversary's zero
    is the readable-streams predicate filtering it inside Postgres, not an indexing
    accident.
    """
    o = await do_setup(client)
    ws, owner_uid, owner_did = o["workspace_id"], o["user_id"], o["device_id"]
    adversary = await _invited_user(client, o["token"], role="member", email="mallory@example.com")

    priv = _add_stream(db_session, ws=ws, name="secret", visibility="private")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=priv, user_id=owner_uid))  # owner only
    await db_session.flush()
    secret = await _seed_message(
        db_session, ws=ws, uid=owner_uid, did=owner_did, sid=priv, text="xyzzy classified plans"
    )

    # Adversary (non-member): zero hits for the private-only term.
    assert await _hits(client, adversary["token"], q="xyzzy") == []
    # Owner (member): the very same query finds it — proves the probe is non-vacuous.
    owner_hits = await _hits(client, o["token"], q="xyzzy")
    assert [h["message_id"] for h in owner_hits] == [secret]


async def test_isolation_public_hits_private_absent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A term in a public channel yields hits (non-guest); a private-only term never does."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    adversary = await _invited_user(client, o["token"], role="member", email="eve@example.com")

    pub = _add_stream(db_session, ws=ws, name="general", visibility="public")
    priv = _add_stream(db_session, ws=ws, name="secret", visibility="private")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=priv, user_id=uid))
    await db_session.flush()
    pub_hit = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=pub, text="quarterly widgets announcement"
    )
    await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=priv, text="secret widgets roadmap"
    )

    # Adversary is a non-guest → reads public channels without a membership row.
    hits = await _hits(client, adversary["token"], q="widgets")
    assert [h["message_id"] for h in hits] == [pub_hit]  # public only; private filtered in-query
    assert all(h["stream_id"] != priv for h in hits)


async def test_isolation_guest_only_explicit_membership(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A guest gets hits ONLY from streams it explicitly belongs to — no meta/public leakage."""
    o = await do_setup(client)
    ws, owner_uid, owner_did = o["workspace_id"], o["user_id"], o["device_id"]
    guest = await _invited_user(client, o["token"], role="guest", email="guest@example.com")
    guest_uid = guest["user_id"]
    meta = await fetch_meta_stream_id(db_session, ws)
    assert meta is not None

    pub = _add_stream(db_session, ws=ws, name="general", visibility="public")
    guest_priv = _add_stream(db_session, ws=ws, name="guest-room", visibility="private")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=guest_priv, user_id=guest_uid))  # explicit only
    await db_session.flush()

    # The identical term "zorptoken" in meta, public, and the guest's private stream.
    await _seed_message(
        db_session, ws=ws, uid=owner_uid, did=owner_did, sid=meta, text="zorptoken meta"
    )
    await _seed_message(
        db_session, ws=ws, uid=owner_uid, did=owner_did, sid=pub, text="zorptoken pub"
    )
    guest_hit = await _seed_message(
        db_session, ws=ws, uid=owner_uid, did=owner_did, sid=guest_priv, text="zorptoken private"
    )

    hits = await _hits(client, guest["token"], q="zorptoken")
    # ONLY the explicit-membership stream — no workspace-meta, no public browser.
    assert [h["message_id"] for h in hits] == [guest_hit]
    assert {h["stream_id"] for h in hits} == {guest_priv}


async def test_isolation_ranking_does_not_bypass_predicate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A very-high-rank private message is still never returned to a non-member.

    Proves the predicate gates BEFORE relevance: the private message would top the
    ranking if it leaked, yet the adversary sees only the lower-ranked public hit.
    """
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    adversary = await _invited_user(client, o["token"], role="member", email="mal2@example.com")

    pub = _add_stream(db_session, ws=ws, name="general", visibility="public")
    priv = _add_stream(db_session, ws=ws, name="secret", visibility="private")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=priv, user_id=uid))
    await db_session.flush()
    pub_hit = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=pub, text="raptor sighting"
    )
    # A denser, higher-ranking match — but private.
    await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=priv, text="raptor raptor raptor raptor raptor"
    )

    hits = await _hits(client, adversary["token"], q="raptor")
    assert [h["message_id"] for h in hits] == [pub_hit]


# --- filters ------------------------------------------------------------------


async def test_in_filter_scopes_and_unreadable_is_zero_not_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``in`` scopes to one stream; ``in`` an unreadable stream → 200 empty (no oracle)."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    adversary = await _invited_user(client, o["token"], role="member", email="eve2@example.com")

    pub_a = _add_stream(db_session, ws=ws, name="a", visibility="public")
    pub_b = _add_stream(db_session, ws=ws, name="b", visibility="public")
    priv = _add_stream(db_session, ws=ws, name="secret", visibility="private")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=priv, user_id=uid))
    await db_session.flush()
    hit_a = await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=pub_a, text="kiwi in a")
    await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=pub_b, text="kiwi in b")
    await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=priv, text="kiwi in secret")

    # in= scopes to exactly that channel.
    scoped = await _hits(client, o["token"], q="kiwi", **{"in": pub_a})
    assert [h["message_id"] for h in scoped] == [hit_a]

    # Adversary scoping into the private stream it cannot read: 200 with zero hits —
    # identical to a readable-but-empty stream (no 404, no existence oracle).
    resp = await _search(client, adversary["token"], q="kiwi", **{"in": priv})
    assert resp.status_code == 200
    assert resp.json()["hits"] == []


async def test_from_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    """``from`` filters by author user id."""
    o = await do_setup(client)
    ws, owner_uid, owner_did = o["workspace_id"], o["user_id"], o["device_id"]
    member = await _invited_user(client, o["token"], role="member", email="bob@example.com")
    m_uid, m_did = member["user_id"], member["device_id"]

    pub = _add_stream(db_session, ws=ws, name="general", visibility="public")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=pub, user_id=owner_uid))
    db_session.add(StreamMember(stream_id=pub, user_id=m_uid))
    await db_session.flush()
    owner_msg = await _seed_message(
        db_session, ws=ws, uid=owner_uid, did=owner_did, sid=pub, text="mango from owner"
    )
    member_msg = await _seed_message(
        db_session, ws=ws, uid=m_uid, did=m_did, sid=pub, text="mango from member"
    )

    both = {h["message_id"] for h in await _hits(client, o["token"], q="mango")}
    assert both == {owner_msg, member_msg}
    only_member = await _hits(client, o["token"], q="mango", **{"from": m_uid})
    assert [h["message_id"] for h in only_member] == [member_msg]


async def test_before_after_bounds(client: AsyncClient, db_session: AsyncSession) -> None:
    """``before`` / ``after`` bound ``created_seq`` (the sort basis) correctly."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    sid = await _member_public_channel(db_session, ws=ws, uid=uid, name="general")

    ids_seq: list[str] = []
    for i in range(5):
        ids_seq.append(
            await _seed_message(
                db_session, ws=ws, uid=uid, did=did, sid=sid, text=f"pear number {i}"
            )
        )
    # created_seq is 1..5 in insertion order within this fresh stream.
    all_hits = await _hits(client, o["token"], q="pear", limit=50)
    seq_by_id = {h["message_id"]: h["created_seq"] for h in all_hits}
    assert sorted(seq_by_id.values()) == [1, 2, 3, 4, 5]

    after_hits = await _hits(client, o["token"], q="pear", after=3, limit=50)
    assert sorted(h["created_seq"] for h in after_hits) == [4, 5]
    before_hits = await _hits(client, o["token"], q="pear", before=3, limit=50)
    assert sorted(h["created_seq"] for h in before_hits) == [1, 2]


# --- deleted excluded ---------------------------------------------------------


async def test_deleted_message_never_appears(client: AsyncClient, db_session: AsyncSession) -> None:
    """A soft-deleted message (text redacted) never appears even for a term it once matched."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    sid = await _member_public_channel(db_session, ws=ws, uid=uid, name="general")

    doomed = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="ephemeral wibblewobble note"
    )
    survivor = await _seed_message(
        db_session, ws=ws, uid=uid, did=did, sid=sid, text="lasting wibblewobble memo"
    )
    # Before deletion both match.
    assert {h["message_id"] for h in await _hits(client, o["token"], q="wibblewobble")} == {
        doomed,
        survivor,
    }

    await _delete_message(db_session, ws=ws, uid=uid, did=did, sid=sid, message_id=doomed)

    hits = await _hits(client, o["token"], q="wibblewobble")
    assert [h["message_id"] for h in hits] == [survivor]  # the tombstone is gone


# --- pagination ---------------------------------------------------------------


async def test_pagination_complete_non_overlapping_and_terminates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Keyset paging over ``limit`` returns complete, non-overlapping pages; last is ``null``."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    sid = await _member_public_channel(db_session, ws=ws, uid=uid, name="general")

    seeded: set[str] = set()
    for i in range(5):
        seeded.add(
            await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=sid, text=f"grape {i}")
        )

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        resp = await _search(client, o["token"], q="grape", limit=2, cursor=cursor)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        collected.extend(h["message_id"] for h in body["hits"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10  # guard against a non-terminating walk

    assert len(collected) == len(set(collected)) == 5  # non-overlapping + complete
    assert set(collected) == seeded
    assert pages == 3  # 2 + 2 + 1, last page exhausts → next_cursor null


async def test_cursor_cannot_reach_unreadable_data(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A cursor minted by a member never lets a non-member page into the private stream.

    The adversary replays the OWNER's next_cursor: the readable-streams predicate
    re-applies on the cursor'd page, so no private hit ever appears — the cursor is
    a sort-key position, not an access grant.
    """
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    adversary = await _invited_user(client, o["token"], role="member", email="mal3@example.com")

    pub = _add_stream(db_session, ws=ws, name="general", visibility="public")
    priv = _add_stream(db_session, ws=ws, name="secret", visibility="private")
    await db_session.flush()
    db_session.add(StreamMember(stream_id=priv, user_id=uid))
    await db_session.flush()
    # Interleave public + private so a naive cursor walk would straddle a private row.
    for i in range(3):
        await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=pub, text=f"melon pub {i}")
        await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=priv, text=f"melon priv {i}")

    # Owner pages with limit=1 to obtain a cursor sitting deep in the mixed ordering.
    owner_first = await _search(client, o["token"], q="melon", limit=1)
    owner_cursor = owner_first.json()["next_cursor"]
    assert owner_cursor is not None

    # Adversary replays the owner's cursor: still zero private hits on every page.
    cursor: str | None = owner_cursor
    seen_streams: set[str] = set()
    for _ in range(20):
        resp = await _search(client, adversary["token"], q="melon", limit=1, cursor=cursor)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen_streams.update(h["stream_id"] for h in body["hits"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert priv not in seen_streams  # the cursor never paged the adversary into private data


# --- empty / stopword / errors ------------------------------------------------


async def test_empty_and_stopword_query_returns_empty_not_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Whitespace / stopword / punctuation queries return an empty page — never a 500."""
    o = await do_setup(client)
    ws, uid, did = o["workspace_id"], o["user_id"], o["device_id"]
    sid = await _member_public_channel(db_session, ws=ws, uid=uid, name="general")
    await _seed_message(db_session, ws=ws, uid=uid, did=did, sid=sid, text="the cat sat")

    for q in ("   ", "the", "!!!", "a of the and"):
        resp = await _search(client, o["token"], q=q)
        assert resp.status_code == 200, (q, resp.text)
        assert resp.json() == {"hits": [], "next_cursor": None}, q


async def test_missing_q_is_422(client: AsyncClient) -> None:
    """A missing required ``q`` is a framework 422 problem+json."""
    o = await do_setup(client)
    resp = await client.get("/v1/search", headers=auth_header(o["token"]))
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_malformed_cursor_is_422(client: AsyncClient) -> None:
    """A malformed ``cursor`` collapses to ``422 /problems/invalid-cursor`` (no internals leak)."""
    o = await do_setup(client)
    for bad in ("not-base64!!", "", "eyJ4Ijoxfq=="):  # junk, empty, valid-b64 wrong-shape JSON-ish
        resp = await _search(client, o["token"], q="anything", cursor=bad)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["type"] == "/problems/invalid-cursor", bad


async def test_search_requires_auth(client: AsyncClient) -> None:
    """Unauthenticated search is a uniform 401."""
    resp = await client.get("/v1/search", params={"q": "hello"})
    assert resp.status_code == 401
