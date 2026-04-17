import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.rfq import RfqSubStatus


class DiscussionMessage(Base):
    __tablename__ = "discussion_messages"
    __table_args__ = (
        Index(
            "ix_discussion_messages_rfq_phase_created_at",
            "rfq_id",
            "phase",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    rfq_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("rfq.rfq_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    phase: Mapped[RfqSubStatus] = mapped_column(
        SAEnum(RfqSubStatus, name="rfqsubstatus", create_type=False),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
