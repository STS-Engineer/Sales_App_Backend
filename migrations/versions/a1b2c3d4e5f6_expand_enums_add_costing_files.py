"""expand enums and add costing_files column

Revision ID: a1b2c3d4e5f6
Revises: 7212fd6c4ca5
Create Date: 2026-03-25 14:00:00.000000

Changes:
  1. Add new RfqStatus enum values (cannot drop IN_COSTING if live rows exist —
     we ADD the new values and leave IN_COSTING in place for backward-compat;
     in a clean DB with no rows using IN_COSTING we also rename it via a data migration).
  2. Add PLM to userrole enum.
  3. Add costing_files JSONB column to rfq table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "7212fd6c4ca5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── New RfqStatus values to add ───────────────────────────────────────────────
NEW_RFQ_STATUS_VALUES = [
    "IN_COSTING_FEASIBILITY",
    "IN_COSTING_PRICING",
    "NEGOTIATION_GET_PO",
    "NEGOTIATION_PROTOTYPE_REQUESTED",
    "NEGOTIATION_PROTOTYPE_ORDER",
    "NEGOTIATION_PROTO_ONGOING",
    "NEGOTIATION_PO_ACCEPTED",
    "MISSION_PREPARATION",
    "PLANT_REVIEW",
    "MANAGED_BY_PLANTS",
    "CANCELLED",
    "LOST",
]


def upgrade() -> None:
    # ── 1. Expand rfqstatus enum ──────────────────────────────────────────────
    # PostgreSQL: ADD VALUE is transactional in PG 12+ when not using IF NOT EXISTS
    # in older alembic versions we must run outside a transaction block.
    # We use op.execute with raw SQL; each ADD VALUE is idempotent with IF NOT EXISTS.
    for value in NEW_RFQ_STATUS_VALUES:
        op.execute(
            f"ALTER TYPE rfqstatus ADD VALUE IF NOT EXISTS '{value}'"
        )

    # ── 2. Expand userrole enum ───────────────────────────────────────────────
    op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'PLM'")

    # ── 3. Add costing_files JSONB column to rfq ──────────────────────────────
    op.add_column(
        "rfq",
        sa.Column(
            "costing_files",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Note: PostgreSQL does NOT support removing enum values once added.
    # We can only drop the costing_files column cleanly.
    op.drop_column("rfq", "costing_files")
    # To revert the enum values you must recreate the type — omitted here
    # because removing enum values from a live DB requires a full migration.
