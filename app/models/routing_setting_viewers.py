from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.product_line_routing import ProductLineRoutingRole


class RoutingSettingViewer(Base):
    __tablename__ = "routing_setting_viewers"
    __table_args__ = (
        UniqueConstraint(
            "product_line",
            "role",
            "user_email",
            name="uq_routing_setting_viewer",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_line: Mapped[str] = mapped_column(
        String,
        ForeignKey("validation_matrix.product_line", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[ProductLineRoutingRole] = mapped_column(
        SAEnum(ProductLineRoutingRole, name="productlineroutingrole"),
        nullable=False,
    )
    user_email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
