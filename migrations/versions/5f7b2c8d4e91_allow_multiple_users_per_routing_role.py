"""allow multiple users per product line routing role

Revision ID: 5f7b2c8d4e91
Revises: 4e6a1c9b2d3f
Create Date: 2026-06-24 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5f7b2c8d4e91"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_product_line_routing_product_line_role",
        "product_line_routing",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_product_line_routing_product_line_role_email",
        "product_line_routing",
        ["product_line", "role", "email"],
    )


def downgrade() -> None:
    # Remove duplicate (product_line, role) rows, keeping the lowest id, before restoring old constraint
    op.execute(
        sa.text(
            "DELETE FROM product_line_routing "
            "WHERE id NOT IN ("
            "  SELECT MIN(id) FROM product_line_routing GROUP BY product_line, role"
            ")"
        )
    )
    op.drop_constraint(
        "uq_product_line_routing_product_line_role_email",
        "product_line_routing",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_product_line_routing_product_line_role",
        "product_line_routing",
        ["product_line", "role"],
    )
