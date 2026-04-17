"""add costing file state and costing recipient email

Revision ID: 6b8f4c2d1e9a
Revises: 3fc6ea7ac7e6
Create Date: 2026-04-16 11:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "6b8f4c2d1e9a"
down_revision: Union[str, None] = "3fc6ea7ac7e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "rfq",
        sa.Column(
            "costing_file_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_messages",
        sa.Column("recipient_email", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussion_messages", "recipient_email")
    op.drop_column("rfq", "costing_file_state")
