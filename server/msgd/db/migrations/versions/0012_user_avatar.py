"""user avatar

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-10 00:00:00.000000

Additive, non-breaking (ENG-152 profile pictures). Adds ``users.avatar_sha256``
— the content-addressed blob digest of the user's SERVER-RE-ENCODED profile
picture, set only by ``POST /v1/me/avatar`` (never the raw upload's hash) and
cleared to NULL by ``DELETE /v1/me/avatar``. Nullable: NULL means "no avatar"
(every pre-existing row, and every cleared avatar). Paired with the ``User``
model change so the permanent ``test_migrations`` ``compare_metadata`` drift
gate stays green.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_sha256", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_sha256")
