"""
Alembic script.py.mako template — do not edit manually.
"""
"""merge heads

Revision ID: 9b0bd5264ee3
Revises: 2a7c9d4e5f10, 5b7e1c2d9f0a
Create Date: 2026-05-05 11:06:47.871989

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b0bd5264ee3'
down_revision: Union[str, None] = ('2a7c9d4e5f10', '5b7e1c2d9f0a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
