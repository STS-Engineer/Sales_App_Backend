"""rename in_validation to pending_for_validation

Revision ID: c4e1f8a9b2d0
Revises: b7d9c2e4f1a0
Create Date: 2026-03-31 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c4e1f8a9b2d0"
down_revision: Union[str, None] = "b7d9c2e4f1a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumlabel = 'IN_VALIDATION'
                  AND enumtypid = to_regtype('rfqsubstatus')
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumlabel = 'PENDING_FOR_VALIDATION'
                  AND enumtypid = to_regtype('rfqsubstatus')
            ) THEN
                ALTER TYPE rfqsubstatus RENAME VALUE 'IN_VALIDATION' TO 'PENDING_FOR_VALIDATION';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumlabel = 'PENDING_FOR_VALIDATION'
                  AND enumtypid = to_regtype('rfqsubstatus')
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumlabel = 'IN_VALIDATION'
                  AND enumtypid = to_regtype('rfqsubstatus')
            ) THEN
                ALTER TYPE rfqsubstatus RENAME VALUE 'PENDING_FOR_VALIDATION' TO 'IN_VALIDATION';
            END IF;
        END
        $$;
        """
    )
