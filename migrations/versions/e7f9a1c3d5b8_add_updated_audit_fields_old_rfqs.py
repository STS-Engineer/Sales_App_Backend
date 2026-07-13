"""add updated_at/updated_by audit fields to old_rfqs_monday and old_rfq_subitems

Revision ID: e7f9a1c3d5b8
Revises: b4e6f8a0c2d4
Create Date: 2026-07-13 00:05:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e7f9a1c3d5b8"
down_revision = "b4e6f8a0c2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "old_rfqs_monday",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.add_column(
        "old_rfqs_monday",
        sa.Column("updated_by", sa.Text(), nullable=True),
        schema="public",
    )
    op.add_column(
        "old_rfq_subitems",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.add_column(
        "old_rfq_subitems",
        sa.Column("updated_by", sa.Text(), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("old_rfq_subitems", "updated_by", schema="public")
    op.drop_column("old_rfq_subitems", "updated_at", schema="public")
    op.drop_column("old_rfqs_monday", "updated_by", schema="public")
    op.drop_column("old_rfqs_monday", "updated_at", schema="public")
