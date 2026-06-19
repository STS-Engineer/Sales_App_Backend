import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class KpiAnnualTarget(Base):
    __tablename__ = "kpi_annual_target"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    year: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    total_ca_meur: Mapped[float | None] = mapped_column(Float, nullable=True)
    renewal_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=25.0, server_default="25.0"
    )
    rfq_automotive_monthly_target: Mapped[int] = mapped_column(
        Integer, nullable=False, default=40, server_default="40"
    )
    rfq_non_auto_monthly_target: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8, server_default="8"
    )
    new_business_monthly_keur: Mapped[float] = mapped_column(
        Float, nullable=False, default=2000.0, server_default="2000.0"
    )
    # [{name: str, ca_meur: float}]
    excluded_zones: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # [{name: str, ca_meur: float}]
    sites: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # [{identifier: str, label: str, annual_keur: float}]
    salesperson_targets: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
