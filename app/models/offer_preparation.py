from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.rfq import Rfq


class OfferPreparation(Base):
    __tablename__ = "offer_preparation"

    rfq_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("rfq.rfq_id", ondelete="CASCADE"),
        primary_key=True,
    )
    offer_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    chat_history: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    rfq: Mapped["Rfq"] = relationship(back_populates="offer_preparation")
