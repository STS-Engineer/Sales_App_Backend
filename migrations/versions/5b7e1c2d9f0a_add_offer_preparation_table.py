"""add offer preparation table and backfill legacy offer chat data

Revision ID: 5b7e1c2d9f0a
Revises: f3c1b7a9d2e4
Create Date: 2026-05-05 15:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "5b7e1c2d9f0a"
down_revision: Union[str, None] = "f3c1b7a9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "offer_preparation",
        sa.Column("rfq_id", sa.String(), nullable=False),
        sa.Column(
            "offer_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "chat_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["rfq_id"], ["rfq.rfq_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rfq_id"),
    )

    op.execute(
        """
        INSERT INTO offer_preparation (rfq_id, offer_data, chat_history)
        SELECT
            rfq_id,
            CASE
                WHEN jsonb_typeof(rfq_data -> 'offer_preparation_data') = 'object'
                    THEN rfq_data -> 'offer_preparation_data'
                ELSE NULL
            END,
            CASE
                WHEN jsonb_typeof(rfq_data -> 'offer_chat_history') = 'array'
                    THEN rfq_data -> 'offer_chat_history'
                ELSE NULL
            END
        FROM rfq
        WHERE
            jsonb_typeof(rfq_data -> 'offer_preparation_data') = 'object'
            OR jsonb_typeof(rfq_data -> 'offer_chat_history') = 'array'
        """
    )

    op.execute(
        """
        UPDATE rfq
        SET rfq_data = CASE
            WHEN rfq_data IS NULL THEN NULL
            ELSE rfq_data - 'offer_preparation_data' - 'offer_chat_history'
        END
        WHERE
            jsonb_typeof(rfq_data -> 'offer_preparation_data') = 'object'
            OR jsonb_typeof(rfq_data -> 'offer_chat_history') = 'array'
        """
    )


def downgrade() -> None:
    op.drop_table("offer_preparation")
