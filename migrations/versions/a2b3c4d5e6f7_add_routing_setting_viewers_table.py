"""add routing_setting_viewers table for read-only viewer access per product line

Revision ID: a2b3c4d5e6f7
Revises: 5f7b2c8d4e91
Create Date: 2026-06-26 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "5f7b2c8d4e91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Reuse the existing enum — do NOT recreate it
_role_enum = postgresql.ENUM(
    "PLM", "RND", "COSTING",
    name="productlineroutingrole",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "routing_setting_viewers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_line", sa.String(), nullable=False),
        sa.Column("role", _role_enum, nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_line"],
            ["validation_matrix.product_line"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_line",
            "role",
            "user_email",
            name="uq_routing_setting_viewer",
        ),
    )
    op.create_index(
        "ix_routing_setting_viewers_product_line",
        "routing_setting_viewers",
        ["product_line"],
    )
    op.create_index(
        "ix_routing_setting_viewers_user_email",
        "routing_setting_viewers",
        ["user_email"],
    )


def downgrade() -> None:
    op.drop_index("ix_routing_setting_viewers_user_email", table_name="routing_setting_viewers")
    op.drop_index("ix_routing_setting_viewers_product_line", table_name="routing_setting_viewers")
    op.drop_table("routing_setting_viewers")
