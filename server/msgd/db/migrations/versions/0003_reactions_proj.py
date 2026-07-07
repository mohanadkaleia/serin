"""reactions_proj

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-06 00:00:00.000000

Additive, non-breaking (ENG-97, M3). Adds the ``reactions_proj`` set projection:
one row per ``(message_id, author_user_id, emoji)`` membership, from which the
aggregated ``(message_id, emoji) -> count`` and who-reacted list are pure
derivations.

**Opaque-bytes emoji (ENG-96 security note).** ``emoji`` is declared ``TEXT
COLLATE "C"`` so the uniqueness key ``(message_id, author_user_id, emoji)`` — the
composite primary key — compares byte-exactly: a deterministic ``C`` collation
never merges two distinct emoji byte sequences (no locale/ICU canonical
equivalence) and always dedups identical bytes. Paired with the ``ReactionProj``
model so the permanent ``test_migrations`` ``compare_metadata`` gate stays green.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reactions_proj",
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Text(), nullable=False),
        # Opaque bytes: C collation → byte-exact uniqueness on the membership key.
        sa.Column("emoji", sa.Text(collation="C"), nullable=False),
        sa.PrimaryKeyConstraint(
            "message_id", "author_user_id", "emoji", name=op.f("pk_reactions_proj")
        ),
    )
    op.create_index(
        "ix_reactions_proj_message_emoji",
        "reactions_proj",
        ["message_id", "emoji"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reactions_proj_message_emoji", table_name="reactions_proj")
    op.drop_table("reactions_proj")
