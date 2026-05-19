import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProductLineRoutingRole(str, enum.Enum):
    PLM = "PLM"
    RND = "RND"
    COSTING = "COSTING"


class ProductLineRouting(Base):
    __tablename__ = "product_line_routing"
    __table_args__ = (
        UniqueConstraint(
            "product_line",
            "role",
            name="uq_product_line_routing_product_line_role",
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
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
