"""add validation audit fields

Revision ID: e2b4c6d8f0a1
Revises: d9a3f4b6c2e1
Create Date: 2026-04-01 12:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e2b4c6d8f0a1"
down_revision = "d9a3f4b6c2e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rfq", sa.Column("validated_by", sa.String(), nullable=True))
    op.add_column("rfq", sa.Column("validation_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rfq", "validation_notes")
    op.drop_column("rfq", "validated_by")
