"""add explication and comment columns to old_rfq_subitems

Revision ID: b3f5a7c9d1e2
Revises: a3d7e5f9c1b2
Create Date: 2026-07-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b3f5a7c9d1e2"
down_revision: Union[str, None] = "a3d7e5f9c1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("old_rfq_subitems", sa.Column("explication", sa.Text(), nullable=True), schema="public")
    op.add_column("old_rfq_subitems", sa.Column("comment", sa.Text(), nullable=True), schema="public")


def downgrade() -> None:
    op.drop_column("old_rfq_subitems", "comment", schema="public")
    op.drop_column("old_rfq_subitems", "explication", schema="public")
