"""
Alembic script.py.mako template — do not edit manually.
"""
"""Merge conflicting heads

Revision ID: 3fc6ea7ac7e6
Revises: 1f7b9c3d4e5a, 9c5d8e7f6a1b
Create Date: 2026-04-15 14:17:57.288515

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3fc6ea7ac7e6'
down_revision: Union[str, None] = ('1f7b9c3d4e5a', '9c5d8e7f6a1b')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
