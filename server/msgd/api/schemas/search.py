"""Response schemas + page constants for ``GET /v1/search`` (ENG-122, TDD §8).

One hit shape plus the page envelope and the search-page caps.  A hit is a
projection of a readable, non-deleted ``messages_proj`` row that matched the
full-text query, carrying its ``ts_rank_cd`` relevance score for client-side
display/sorting.  ``next_cursor`` is the opaque keyset token for the next page
(``null`` when the result set is exhausted).

:data:`DEFAULT_LIMIT` / :data:`MAX_LIMIT` encode the §8 search-page cap; the
router clamps a client ``limit`` into ``[MIN_LIMIT, MAX_LIMIT]`` in code (never
via ``Query(ge/le)``, which would 422 instead of clamp — the ENG-67 pull-page
idiom).  The cap is deliberately smaller than the pull-page cap: a search page is
a ranked slice for a human to scan, not a bulk catch-up.
"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "SearchHit",
    "SearchResponse",
]

#: §8 search-page cap. A search page is a human-scannable ranked slice, so the
#: default is small and the ceiling modest; a client ``limit`` is clamped into
#: ``[MIN_LIMIT, MAX_LIMIT]`` in the router.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MIN_LIMIT = 1


class SearchHit(BaseModel):
    """One readable, non-deleted message that matched the query.

    ``text`` is the full stored message text for MVP (a ``ts_headline`` snippet is
    a documented future refinement — plain text leaks nothing the caller could not
    already read, since the row survived the readable-streams predicate).  ``rank``
    is the ``ts_rank_cd`` relevance score, returned per row so the client can
    surface relevance even though the server orders by recency (see the router
    docstring for the ordering/pagination rationale).  ``thread_root_id`` is
    ``null`` for a top-level message and set for a threaded reply.
    """

    message_id: str
    stream_id: str
    author_user_id: str
    text: str
    created_seq: int
    rank: float
    thread_root_id: str | None = None


class SearchResponse(BaseModel):
    """One page of search hits + the opaque next-page keyset cursor.

    ``next_cursor`` is ``null`` exactly when this page exhausts the result set; it
    encodes only sort-key values ``(created_seq, message_id)`` and carries no
    access grant — the readable-streams predicate re-applies on every page, so a
    cursor can never page a caller into data it may not read.
    """

    hits: list[SearchHit]
    next_cursor: str | None = None
