"""add systematic_rfq_id to audit_logs

Revision ID: b4e6f8a0c2d4
Revises: a3d7e5f9c1b2
Create Date: 2026-07-07 17:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b4e6f8a0c2d4"
down_revision: Union[str, None] = "a3d7e5f9c1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("systematic_rfq_id", sa.String(), nullable=True))
    op.create_index(
        "ix_audit_logs_systematic_rfq_id",
        "audit_logs",
        ["systematic_rfq_id"],
    )

    # Backfill existing rows from the linked RFQ so historical entries also
    # carry the human-readable reference, not just future ones.
    op.execute(
        """
        UPDATE audit_logs
        SET systematic_rfq_id = rfq.systematic_rfq_id
        FROM rfq
        WHERE audit_logs.rfq_id = rfq.rfq_id
          AND rfq.systematic_rfq_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_systematic_rfq_id", table_name="audit_logs")
    op.drop_column("audit_logs", "systematic_rfq_id")
