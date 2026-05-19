"""add product line routing table

Revision ID: 4e6a1c9b2d3f
Revises: e6f4a2b9c8d1
Create Date: 2026-05-19 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4e6a1c9b2d3f"
down_revision: Union[str, None] = "e6f4a2b9c8d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROUTING_ROLE_ENUM = sa.Enum(
    "PLM",
    "RND",
    "COSTING",
    name="productlineroutingrole",
)

SEEDED_ROUTING_ROWS = [
    ("Chokes", "COSTING", "ons.ghariani@avocarbon.com"),
    ("Chokes", "PLM", "ons.ghariani@avocarbon.com"),
    ("Chokes", "RND", "ons.ghariani@avocarbon.com"),
    ("Brushes", "COSTING", "ons.ghariani@avocarbon.com"),
    ("Brushes", "PLM", "ons.ghariani@avocarbon.com"),
    ("Brushes", "RND", "ons.ghariani@avocarbon.com"),
    ("Seals", "COSTING", "ons.ghariani@avocarbon.com"),
    ("Seals", "PLM", "ons.ghariani@avocarbon.com"),
    ("Seals", "RND", "ons.ghariani@avocarbon.com"),
    ("Assembly", "COSTING", "ons.ghariani@avocarbon.com"),
    ("Assembly", "PLM", "ons.ghariani@avocarbon.com"),
    ("Assembly", "RND", "ons.ghariani@avocarbon.com"),
    ("Advanced material", "COSTING", "ons.ghariani@avocarbon.com"),
    ("Advanced material", "PLM", "ons.ghariani@avocarbon.com"),
    ("Advanced material", "RND", "ons.ghariani@avocarbon.com"),
    ("Friction", "COSTING", "ons.ghariani@avocarbon.com"),
    ("Friction", "PLM", "ons.ghariani@avocarbon.com"),
    ("Friction", "RND", "ons.ghariani@avocarbon.com"),
]


def upgrade() -> None:
    op.create_table(
        "product_line_routing",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "product_line",
            sa.String(),
            sa.ForeignKey("validation_matrix.product_line", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", ROUTING_ROLE_ENUM, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "product_line",
            "role",
            name="uq_product_line_routing_product_line_role",
        ),
    )
    op.create_index(
        "ix_product_line_routing_product_line",
        "product_line_routing",
        ["product_line"],
        unique=False,
    )
    op.create_index(
        "ix_product_line_routing_email",
        "product_line_routing",
        ["email"],
        unique=False,
    )

    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT product_line FROM validation_matrix"))
    valid_product_lines = {row[0] for row in result}

    formatted_seed_data = [
        {"pl": product_line, "role": role, "email": email}
        for product_line, role, email in SEEDED_ROUTING_ROWS
    ]
    filtered_rows = [
        row for row in formatted_seed_data if row["pl"] in valid_product_lines
    ]

    insert_statement = sa.text(
        "INSERT INTO product_line_routing (product_line, role, email) "
        "VALUES (:pl, CAST(:role AS productlineroutingrole), :email)"
    )
    for row in filtered_rows:
        op.execute(insert_statement.bindparams(**row))


def downgrade() -> None:
    op.drop_index("ix_product_line_routing_email", table_name="product_line_routing")
    op.drop_index("ix_product_line_routing_product_line", table_name="product_line_routing")
    op.drop_table("product_line_routing")
    ROUTING_ROLE_ENUM.drop(op.get_bind(), checkfirst=False)
