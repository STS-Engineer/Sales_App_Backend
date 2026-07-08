"""move explication/comment from old_rfq_subitems to old_rfqs_monday

Revision ID: c4d6e8f0a2b3
Revises: b3f5a7c9d1e2
Create Date: 2026-07-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4d6e8f0a2b3"
down_revision: Union[str, None] = "b3f5a7c9d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("old_rfq_subitems", "comment", schema="public")
    op.drop_column("old_rfq_subitems", "explication", schema="public")
    op.add_column("old_rfqs_monday", sa.Column("explication", sa.Text(), nullable=True), schema="public")
    op.add_column("old_rfqs_monday", sa.Column("comment", sa.Text(), nullable=True), schema="public")


def downgrade() -> None:
    op.drop_column("old_rfqs_monday", "comment", schema="public")
    op.drop_column("old_rfqs_monday", "explication", schema="public")
    op.add_column("old_rfq_subitems", sa.Column("explication", sa.Text(), nullable=True), schema="public")
    op.add_column("old_rfq_subitems", sa.Column("comment", sa.Text(), nullable=True), schema="public")
