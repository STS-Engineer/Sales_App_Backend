"""rename validator role to zone manager

Revision ID: b7d9c2e4f1a0
Revises: a1b2c3d4e5f6
Create Date: 2026-03-31 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7d9c2e4f1a0"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
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
                WHERE enumlabel = 'VALIDATOR'
                  AND enumtypid = 'userrole'::regtype
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumlabel = 'ZONE_MANAGER'
                  AND enumtypid = 'userrole'::regtype
            ) THEN
                ALTER TYPE userrole RENAME VALUE 'VALIDATOR' TO 'ZONE_MANAGER';
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
                WHERE enumlabel = 'ZONE_MANAGER'
                  AND enumtypid = 'userrole'::regtype
            ) AND NOT EXISTS (
                SELECT 1
                FROM pg_enum
                WHERE enumlabel = 'VALIDATOR'
                  AND enumtypid = 'userrole'::regtype
            ) THEN
                ALTER TYPE userrole RENAME VALUE 'ZONE_MANAGER' TO 'VALIDATOR';
            END IF;
        END
        $$;
        """
    )
