"""add last_login and operating_system columns to users

Revision ID: f1a3c5e7b9d0
Revises: e7f9a1c3d5b8
Create Date: 2026-07-13 00:10:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f1a3c5e7b9d0"
down_revision = "e7f9a1c3d5b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("operating_system", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "operating_system")
    op.drop_column("users", "last_login")
