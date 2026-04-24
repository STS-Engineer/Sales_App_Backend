"""add RND to the userrole enum

Revision ID: 8c1e2d7f4b9a
Revises: 6b8f4c2d1e9a
Create Date: 2026-04-24 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "8c1e2d7f4b9a"
down_revision: Union[str, None] = "6b8f4c2d1e9a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'RND'")


def downgrade() -> None:
    # PostgreSQL does not support dropping enum values directly.
    pass
