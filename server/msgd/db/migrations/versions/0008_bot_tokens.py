"""bot tokens

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-09 00:00:00.000000

Additive, non-breaking (ENG-159, M5-1). Adds the ``bot_tokens`` table: scoped
bot bearer credentials keyed by the sha256 hex of the raw token (the
``sessions``/``invites`` D2 discipline — the raw token is returned once at mint
and never persisted). No ``expires_at``: a bot credential lives until revoked
(``revoked_at`` tombstone) or its bot user is deactivated (hard bulk-delete in
the admin deactivation branch, mirroring the session bulk-revoke). ``scopes``
is the JSONB verb-scope list (``events:read``/``events:write``/``files:write``,
§10) that ``require_auth`` surfaces as ``AuthContext.scopes``.
``ix_bot_tokens_bot_user_id`` backs the deactivation bulk-revoke and the
plugins listing (the ``ix_sessions_user_id`` precedent). Paired with the
``BotToken`` model so the permanent ``test_migrations`` ``compare_metadata``
drift gate stays green.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("bot_user_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("scopes", JSONB(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["bot_user_id"], ["users.user_id"], name=op.f("fk_bot_tokens_bot_user_id_users")
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_bot_tokens")),
    )
    op.create_index("ix_bot_tokens_bot_user_id", "bot_tokens", ["bot_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bot_tokens_bot_user_id", table_name="bot_tokens")
    op.drop_table("bot_tokens")
