"""add validation action timestamps

Revision ID: d9a3f4b6c2e1
Revises: c4e1f8a9b2d0
Create Date: 2026-04-01 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d9a3f4b6c2e1"
down_revision: Union[str, None] = "c4e1f8a9b2d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rfq", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("rfq", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("rfq", "rejected_at")
    op.drop_column("rfq", "approved_at")
