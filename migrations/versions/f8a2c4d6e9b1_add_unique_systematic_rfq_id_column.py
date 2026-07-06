"""add unique systematic_rfq_id column to rfq

Revision ID: f8a2c4d6e9b1
Revises: c7d9e1f2a3b4
Create Date: 2026-07-06 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f8a2c4d6e9b1"
down_revision: Union[str, None] = "c7d9e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rfq", sa.Column("systematic_rfq_id", sa.String(), nullable=True))

    # Backfill from the existing JSON field so the column reflects current data.
    op.execute(
        """
        UPDATE rfq
        SET systematic_rfq_id = rfq_data ->> 'systematic_rfq_id'
        WHERE rfq_data ? 'systematic_rfq_id'
          AND rfq_data ->> 'systematic_rfq_id' <> ''
        """
    )

    # Partial unique index: NULLs (draft / Potential rows without a number yet)
    # are allowed to repeat, but any assigned reference must be unique.
    op.create_index(
        "ix_rfq_systematic_rfq_id_unique",
        "rfq",
        ["systematic_rfq_id"],
        unique=True,
        postgresql_where=sa.text("systematic_rfq_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_rfq_systematic_rfq_id_unique", table_name="rfq")
    op.drop_column("rfq", "systematic_rfq_id")
