"""``GET /v1/search`` — Postgres FTS message search, readable-scoped (ENG-122, §8).

THE SECURITY CRUX — PERMISSION ISOLATION.  A search MUST return zero hits from
any stream the caller cannot read.  This is enforced by joining the **one shared**
:func:`~msgd.events.permissions.readable_streams_predicate` (the exact fragment
pull/sync/fanout reuse) straight into the ``WHERE`` clause of the single search
statement — never a post-filter in Python.  Because the predicate is a live
boolean over ``streams`` + an ``EXISTS(stream_members)`` branch, an unreadable
message is filtered **inside Postgres** and never leaves the database, regardless
of how relevant it ranks.  There is no second access check here and no
fetch-then-filter: the SQL itself is the whole gate.  Consequences that fall out
of reusing the predicate verbatim (D5):

* a **non-member** of a private channel / DM gets zero hits from it — a term that
  appears ONLY in that private stream returns nothing;
* a **guest** gets hits ONLY from streams it has an explicit ``stream_members``
  row in — no ``workspace-meta`` / public-channel leakage (the FLAGGED DEVIATION);
* filtering with ``in=`` to a stream the caller cannot read yields zero rows via
  the same predicate — NOT a 404 and NOT an error, so there is **no existence
  oracle**: an unreadable ``in`` target is indistinguishable from a readable one
  that simply held no match.

Query construction (all in ONE SQL statement):

1. **FTS match** — ``websearch_to_tsquery('english', :q)`` matched against the
   STORED GENERATED ``messages_proj.search_tsv`` (``search_tsv @@ query``), scored
   with ``ts_rank_cd``.  ``websearch_to_tsquery`` never raises on hostile input
   (unbalanced quotes, operators, punctuation) and yields the **empty** tsquery
   for stopword-/punctuation-only input; an empty tsquery ``@@`` matches nothing,
   so such a query returns an empty page — never a 500 (§8).
2. **Permission scope** — ``messages_proj JOIN streams`` + the readable-streams
   predicate + an explicit ``streams.workspace_id == ctx.workspace_id`` (the
   predicate already constrains the workspace; kept explicit as defense in depth).
3. **Exclusions** — ``deleted == False``.  A soft-deleted message has its text
   redacted to ``''`` (so its ``search_tsv`` is empty and cannot match), but the
   predicate is stated explicitly anyway — a tombstone never surfaces old content.
4. **Filters** — optional ``in`` (a ``stream_id``), ``from`` (an author user id),
   and ``before`` / ``after`` (``created_seq`` bounds, the sort basis — see below).

Ordering + pagination (the CHOSEN, stable model — documented deviation from the
ticket's rank-first keyset).  The result set is ordered ``created_seq DESC,
message_id DESC`` — a **total** order: ``created_seq`` is the per-stream accept
sequence and ``message_id`` (the global ``messages_proj`` primary key) is the
final tiebreak, so ties — including cross-stream ``created_seq`` collisions, since
``created_seq`` is per-stream not a global clock — are broken deterministically.
Keyset (``search_after``) pagination walks that order with a clean
``(created_seq, message_id)`` cursor, so every page is non-overlapping and
complete and the walk is O(page) (no OFFSET).  Rank-first keyset was deliberately
**rejected**: ``ts_rank_cd`` returns a float, and a float-equality tiebreak in the
keyset predicate is fragile across pages (serialize/parse/compare precision) — a
correctness hazard the recency-first order sidesteps entirely.  ``rank`` is still
computed and returned per hit for client-side relevance display/sorting; only the
*server* page order is recency-first.  The cursor encodes ONLY the two sort-key
values and grants nothing: the readable-streams predicate re-applies on every
page, so a hand-crafted or replayed cursor can never reach unreadable data.

Auth: :data:`~msgd.api.deps.CurrentAuth`.  A per-user rate limit
(:func:`~msgd.api.deps.search_rate_limit`, keyed ``user:{user_id}``) guards the
endpoint — FTS is a cheap-ish read but not free, so it is budgeted like the event
/ file reads (ENG-66 / ENG-116 idiom).  Errors are problem+json: a missing ``q``
is a framework 422; a malformed ``cursor`` is a ``422 /problems/invalid-cursor``.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.api.deps import CurrentAuth, search_rate_limit
from msgd.api.problems import ProblemException
from msgd.api.schemas.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    SearchHit,
    SearchResponse,
)
from msgd.db.engine import get_session
from msgd.db.models import MessageProj, Stream
from msgd.events.permissions import readable_streams_predicate

router = APIRouter(prefix="/v1", tags=["search"])

DbSession = Annotated[AsyncSession, Depends(get_session)]

#: Text-search configuration for both the query parse and the stored ``search_tsv``
#: (``messages_proj.search_tsv`` is ``to_tsvector('english', text)``); the two MUST
#: agree or a match would silently never fire.
_TS_CONFIG = "english"


def _invalid_cursor() -> ProblemException:
    """A ``cursor`` that is not a well-formed keyset token → 422.

    Constructed inline (not a ``problems`` factory) because ``problems.py`` is a
    shared file this ticket does not edit — the ENG-67 ``_invalid_cursor`` idiom.
    The detail is deliberately generic: a cursor carries only opaque sort-key
    values, so there is nothing sensitive to disclose, but nothing is echoed back.
    """
    return ProblemException(
        status=422,
        type="/problems/invalid-cursor",
        title="Invalid cursor",
        detail="cursor is malformed",
    )


def _encode_cursor(created_seq: int, message_id: str) -> str:
    """Encode a ``(created_seq, message_id)`` keyset position as an opaque token.

    Base64url of a tiny JSON object.  The token is NOT a capability — it encodes
    only the two sort-key values of the last row on the page; the readable-streams
    predicate re-applies on the next page, so the token grants no access.
    """
    raw = json.dumps({"c": created_seq, "m": message_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    """Decode an opaque cursor back to ``(created_seq, message_id)`` or raise 422.

    EVERY malformed shape (bad base64, bad JSON, missing/mistyped fields) collapses
    to the single ``/problems/invalid-cursor`` — a client cannot probe internals
    through cursor errors, and a tampered cursor is rejected rather than trusted.
    Even a well-formed-but-forged cursor is safe: it only shifts the keyset window;
    the predicate still gates which rows that window can contain.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        parsed: Any = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _invalid_cursor() from exc
    if not isinstance(parsed, dict):
        raise _invalid_cursor()
    created_seq = parsed.get("c")
    message_id = parsed.get("m")
    # ``bool`` is an ``int`` subclass — exclude it so ``true``/``false`` is not a seq.
    if not isinstance(created_seq, int) or isinstance(created_seq, bool):
        raise _invalid_cursor()
    if not isinstance(message_id, str):
        raise _invalid_cursor()
    return created_seq, message_id


@router.get(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(search_rate_limit)],
)
async def get_search(
    ctx: CurrentAuth,
    db: DbSession,
    q: Annotated[str, Query(description="full-text search query")],
    in_: Annotated[str | None, Query(alias="in")] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    before: Annotated[int | None, Query(ge=0)] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query()] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> SearchResponse:
    """Return one readable-scoped, ranked page of full-text message hits (see module docstring).

    ``q`` is required (missing → framework 422).  An empty / stopword-only /
    punctuation-only ``q`` parses to the empty tsquery and returns an empty page
    (never an error).  ``in`` scopes to a single stream (an unreadable target
    simply yields zero rows via the predicate — no 404, no oracle); ``from`` filters
    by author; ``before`` / ``after`` bound ``created_seq`` (strictly, the sort
    basis).  ``limit`` is clamped to ``[MIN_LIMIT, MAX_LIMIT]``; ``cursor`` (opaque)
    resumes the keyset walk — a malformed cursor is a ``422 /problems/invalid-cursor``.
    """
    effective = min(max(limit, MIN_LIMIT), MAX_LIMIT)

    # Short-circuit a whitespace-only query: an empty tsquery matches nothing, so
    # skip the DB round trip entirely (stopword-only text still goes through the
    # query below and returns empty naturally via ``@@`` against the empty tsquery).
    if not q.strip():
        return SearchResponse(hits=[], next_cursor=None)

    tsquery = func.websearch_to_tsquery(_TS_CONFIG, q)
    rank = func.ts_rank_cd(MessageProj.search_tsv, tsquery)

    # THE CRUX: the shared readable-streams predicate joined INTO the WHERE clause.
    # Correlated to the joined ``streams`` row; its EXISTS(stream_members) branch
    # re-evaluates live per query (removal cuts access on the very next search, D13).
    predicate = readable_streams_predicate(
        user_id=ctx.user_id, role=ctx.role, workspace_id=ctx.workspace_id
    )

    conditions: list[ColumnElement[bool]] = [
        MessageProj.search_tsv.bool_op("@@")(tsquery),
        MessageProj.deleted.is_(False),
        # Explicit workspace scope (the predicate already constrains it — defense
        # in depth, and it keeps the intent legible next to the id filters).
        Stream.workspace_id == ctx.workspace_id,
        predicate,
    ]
    if in_ is not None:
        conditions.append(MessageProj.stream_id == in_)
    if from_ is not None:
        conditions.append(MessageProj.author_user_id == from_)
    if after is not None:
        conditions.append(MessageProj.created_seq > after)
    if before is not None:
        conditions.append(MessageProj.created_seq < before)
    if cursor is not None:
        c_seq, c_mid = _decode_cursor(cursor)
        # Keyset over the DESC total order: strictly "after" the cursor position.
        conditions.append(
            or_(
                MessageProj.created_seq < c_seq,
                and_(MessageProj.created_seq == c_seq, MessageProj.message_id < c_mid),
            )
        )

    stmt = (
        select(
            MessageProj.message_id,
            MessageProj.stream_id,
            MessageProj.thread_root_id,
            MessageProj.author_user_id,
            MessageProj.text,
            MessageProj.created_seq,
            rank.label("rank"),
        )
        .select_from(MessageProj)
        .join(Stream, Stream.stream_id == MessageProj.stream_id)
        .where(and_(*conditions))
        .order_by(MessageProj.created_seq.desc(), MessageProj.message_id.desc())
        # Fetch one extra row to decide ``next_cursor`` without a second count query.
        .limit(effective + 1)
    )

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > effective
    page = rows[:effective]

    hits = [
        SearchHit(
            message_id=row.message_id,
            stream_id=row.stream_id,
            author_user_id=row.author_user_id,
            text=row.text,
            created_seq=row.created_seq,
            rank=float(row.rank),
            thread_root_id=row.thread_root_id,
        )
        for row in page
    ]
    next_cursor = (
        _encode_cursor(page[-1].created_seq, page[-1].message_id) if has_more and page else None
    )
    return SearchResponse(hits=hits, next_cursor=next_cursor)
