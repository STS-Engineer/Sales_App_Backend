"""add AI_APPROVED sub-status for Workspace Agent pre-validation

Revision ID: c7d9e1f2a3b4
Revises: a2b3c4d5e6f7
Create Date: 2026-06-29 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c7d9e1f2a3b4"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE rfqsubstatus ADD VALUE IF NOT EXISTS 'AI_APPROVED'")


def downgrade() -> None:
    pass
