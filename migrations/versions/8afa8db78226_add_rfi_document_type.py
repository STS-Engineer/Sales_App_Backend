"""
Alembic script.py.mako template — do not edit manually.
"""
"""add_rfi_document_type

Revision ID: 8afa8db78226
Revises: 9b0bd5264ee3
Create Date: 2026-05-05 16:52:02.824140

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8afa8db78226'
down_revision: Union[str, None] = '9b0bd5264ee3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE rfqsubstatus ADD VALUE IF NOT EXISTS 'RFI_COMPLETED'")

    document_type = postgresql.ENUM("RFQ", "RFI", name="rfqdocumenttype", create_type=False)
    document_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "rfq",
        sa.Column(
            "document_type",
            document_type,
            server_default="RFQ",
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column("rfq", "document_type")
    document_type = postgresql.ENUM("RFQ", "RFI", name="rfqdocumenttype", create_type=False)
    document_type.drop(op.get_bind(), checkfirst=True)
    # PostgreSQL cannot drop enum values in-place; RFI_COMPLETED remains in rfqsubstatus.
