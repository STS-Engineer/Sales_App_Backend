"""add notification logs and RFQ follow-up state

Revision ID: 2a7c9d4e5f10
Revises: 8c1e2d7f4b9a
Create Date: 2026-04-28 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2a7c9d4e5f10"
down_revision: Union[str, None] = "8c1e2d7f4b9a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "rfq",
        sa.Column("last_notification_sent_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "rfq",
        sa.Column(
            "follow_up_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    op.create_table(
        "notification_logs",
        sa.Column("log_id", sa.String(), nullable=False),
        sa.Column("rfq_id", sa.String(), nullable=False),
        sa.Column("recipient_email", sa.String(), nullable=False),
        sa.Column("email_type", sa.String(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["rfq_id"], ["rfq.rfq_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index(
        "ix_notification_logs_rfq_id",
        "notification_logs",
        ["rfq_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_logs_sent_at",
        "notification_logs",
        ["sent_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_logs_email_type",
        "notification_logs",
        ["email_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_logs_email_type", table_name="notification_logs")
    op.drop_index("ix_notification_logs_sent_at", table_name="notification_logs")
    op.drop_index("ix_notification_logs_rfq_id", table_name="notification_logs")
    op.drop_table("notification_logs")
    op.drop_column("rfq", "follow_up_count")
    op.drop_column("rfq", "last_notification_sent_at")
