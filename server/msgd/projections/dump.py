"""``dump_messages_proj`` — the deterministic ``messages_proj`` equivalence surface (ENG-69).

The server analogue of M0's ``dump_messages`` (ENG-58), with the same discipline:
a fixed explicit column list (never ``SELECT *``), one compact JSON object per row
(``ensure_ascii=False``, ``separators=(",", ":")``), ``\\n``-joined, under a total
deterministic ``ORDER BY``.  Raw table bytes are not deterministic; this **text**
is.  Two logs that are equal → a byte-identical dump; a rebuilt projection and an
incrementally-built one over the same log → a byte-identical dump (``rebuild ≡
incremental``, TDD §5 / §12 invariant 6).  Reusable by the M2 simulation suite's
invariant 6, not only by the server equivalence gate.
"""

from __future__ import annotations

import json
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msgd.db.models import MessageProj, ReactionProj

__all__ = ["dump_messages_proj", "dump_reactions_proj"]

#: The dumped columns, in fixed order (never ``SELECT *``).  The M1-relevant
#: subset the apply actually writes.
#:
#: EXCLUDED and why: the later-milestone columns (``edited_seq``, ``deleted``,
#: ``reply_count``, ``last_reply_seq``) are constant defaults at M1 — dumping
#: them proves nothing about the apply logic and would couple the gate to those
#: defaults.  ``search_tsv`` is GENERATED (a pure function of ``text``), not part
#: of the equivalence surface.  ``messages_proj`` has NO ``format`` column (§4.2
#: drops it — a deliberate difference from M0's SQLite ``messages`` table), so,
#: unlike M0's dump, ``format`` is not dumped.
#:
#: When later milestones land edit/delete/thread-counter reducers, EXTEND this
#: list to cover the columns they write so the gate keeps proving those too.
_DUMP_COLUMNS: Final = (
    "message_id",
    "stream_id",
    "thread_root_id",
    "author_user_id",
    "text",
    "created_seq",
)


async def dump_messages_proj(session: AsyncSession) -> str:
    """Return the normalized, deterministic ``messages_proj`` dump.

    ``ORDER BY stream_id, created_seq, message_id``: ``(stream_id, created_seq)``
    is already unique (per-stream ``server_sequence`` is unique), and
    ``message_id`` is a bulletproof final tie-break — a total, stable order, so
    identical logs yield byte-identical dumps.
    """
    rows = await session.execute(
        select(
            MessageProj.message_id,
            MessageProj.stream_id,
            MessageProj.thread_root_id,
            MessageProj.author_user_id,
            MessageProj.text,
            MessageProj.created_seq,
        ).order_by(MessageProj.stream_id, MessageProj.created_seq, MessageProj.message_id)
    )
    return "\n".join(
        json.dumps(
            dict(zip(_DUMP_COLUMNS, row, strict=True)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in rows.all()
    )


#: The dumped ``reactions_proj`` columns, in fixed order — the full membership key
#: (the table IS the reaction set, ENG-97). No later-milestone or derived columns
#: to exclude.
_REACTION_DUMP_COLUMNS: Final = (
    "message_id",
    "author_user_id",
    "emoji",
)


async def dump_reactions_proj(session: AsyncSession) -> str:
    """Return the normalized, deterministic ``reactions_proj`` dump (ENG-97).

    Same discipline as :func:`dump_messages_proj`: a fixed column list, one
    compact JSON object per row, ``\\n``-joined, under a total ``ORDER BY``. The
    order is the membership key ``(message_id, author_user_id, emoji)`` — unique
    (it is the primary key), so the order is total and stable. ``emoji`` orders
    under the column's ``C`` collation (byte order), so the dump is byte-exact and
    two equal reaction sets yield byte-identical dumps (the ``rebuild ≡
    incremental`` equivalence surface for reactions, TDD §5 / §12 invariant 6).
    """
    rows = await session.execute(
        select(
            ReactionProj.message_id,
            ReactionProj.author_user_id,
            ReactionProj.emoji,
        ).order_by(ReactionProj.message_id, ReactionProj.author_user_id, ReactionProj.emoji)
    )
    return "\n".join(
        json.dumps(
            dict(zip(_REACTION_DUMP_COLUMNS, row, strict=True)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in rows.all()
    )
