"""add revision requested status and notes

Revision ID: 1f7b9c3d4e5a
Revises: f3c1b7a9d2e4
Create Date: 2026-04-15 12:15:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "1f7b9c3d4e5a"
down_revision: Union[str, None] = "f3c1b7a9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE rfqsubstatus ADD VALUE IF NOT EXISTS 'REVISION_REQUESTED'")
    op.add_column("rfq", sa.Column("revision_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rfq", "revision_notes")
