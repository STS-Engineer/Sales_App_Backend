"""add discussion messages table

Revision ID: 9c5d8e7f6a1b
Revises: f3c1b7a9d2e4
Create Date: 2026-04-14 13:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9c5d8e7f6a1b"
down_revision: Union[str, None] = "f3c1b7a9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discussion_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("rfq_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(
            "phase",
            postgresql.ENUM(
                "POTENTIAL",
                "NEW_RFQ",
                "PENDING_FOR_VALIDATION",
                "FEASIBILITY",
                "PRICING",
                "PREPARATION",
                "VALIDATION",
                "GET_PO",
                "PO_ACCEPTED",
                "MISSION_ACCEPTED",
                "MISSION_NOT_ACCEPTED",
                "GET_PROTOTYPE",
                "PROTOTYPE_ONGOING",
                "LOST",
                "CANCELED",
                "PO_SECURED",
                name="rfqsubstatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["rfq_id"], ["rfq.rfq_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_discussion_messages_rfq_phase_created_at",
        "discussion_messages",
        ["rfq_id", "phase", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_messages_rfq_phase_created_at",
        table_name="discussion_messages",
    )
    op.drop_table("discussion_messages")
