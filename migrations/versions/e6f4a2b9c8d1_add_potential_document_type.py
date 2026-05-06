"""add potential document type

Revision ID: e6f4a2b9c8d1
Revises: 8afa8db78226
Create Date: 2026-05-06 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e6f4a2b9c8d1"
down_revision: Union[str, None] = "8afa8db78226"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE rfqdocumenttype ADD VALUE IF NOT EXISTS 'POTENTIAL'")

    op.execute(
        """
        UPDATE rfq
        SET document_type = 'POTENTIAL',
            phase = 'RFQ',
            sub_status = 'NEW_RFQ'
        WHERE sub_status = 'POTENTIAL'
        """
    )
    op.execute(
        """
        UPDATE discussion_messages
        SET phase = 'NEW_RFQ'
        WHERE phase = 'POTENTIAL'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE rfq
        SET document_type = 'RFQ'
        WHERE document_type = 'POTENTIAL'
        """
    )
    # PostgreSQL cannot drop enum values in-place; POTENTIAL remains in rfqdocumenttype.
