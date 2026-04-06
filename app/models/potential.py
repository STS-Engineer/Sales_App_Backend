from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.rfq import Rfq


class Potential(Base):
    __tablename__ = "potential"

    rfq_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("rfq.rfq_id", ondelete="CASCADE"),
        primary_key=True,
    )
    potential_systematic_id: Mapped[str | None] = mapped_column(
        String,
        unique=True,
        nullable=True,
    )

    customer: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    application: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_function: Mapped[str | None] = mapped_column(Text, nullable=True)

    industry_served: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_product_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    engagement_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    idea_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_supplier: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_win_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    win_rationale_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    technical_capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategic_fit: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategic_fit_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    sales_keur: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_percentage: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_keur: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_of_production: Mapped[str | None] = mapped_column(Text, nullable=True)
    development_effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    side_effects: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks_to_do: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks_not_to_do: Mapped[str | None] = mapped_column(Text, nullable=True)

    chat_history: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )

    rfq: Mapped["Rfq"] = relationship(back_populates="potential")
