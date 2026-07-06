"""enforce systematic_rfq_id numeric prefix unique across all acronyms

Revision ID: a3d7e5f9c1b2
Revises: f8a2c4d6e9b1
Create Date: 2026-07-06 13:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a3d7e5f9c1b2"
down_revision: Union[str, None] = "f8a2c4d6e9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pre-existing legacy pair that already shares the numeric prefix "26512"
# across two different acronyms (26512-ASS-00 / 26512-BRU-00). Left as-is by
# request. Only the newer of the two (26512-ASS-00) is excluded from the new
# index below — the older one (26512-BRU-00) stays indexed normally, so the
# number "26512" is still correctly reserved and can't be reused by anyone else.
_LEGACY_EXCEPTION_ID = "26512-ASS-00"


def upgrade() -> None:
    op.drop_index("ix_rfq_systematic_rfq_id_unique", table_name="rfq")

    op.create_index(
        "ix_rfq_systematic_number_unique",
        "rfq",
        [sa.text("split_part(systematic_rfq_id, '-', 1)")],
        unique=True,
        postgresql_where=sa.text(
            f"systematic_rfq_id IS NOT NULL AND systematic_rfq_id <> '{_LEGACY_EXCEPTION_ID}'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_rfq_systematic_number_unique", table_name="rfq")
    op.create_index(
        "ix_rfq_systematic_rfq_id_unique",
        "rfq",
        ["systematic_rfq_id"],
        unique=True,
        postgresql_where=sa.text("systematic_rfq_id IS NOT NULL"),
    )
