from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ValidationMatrix(Base):
    """
    Stores the KEUR escalation thresholds per product line.
    The 'acronym' column has a UNIQUE constraint so it can be used
    as a FK target by the rfq.product_line_acronym column.
    """

    __tablename__ = "validation_matrix"
    __table_args__ = (UniqueConstraint("acronym", name="uq_validation_matrix_acronym"),)

    product_line: Mapped[str] = mapped_column(String, primary_key=True)
    acronym: Mapped[str] = mapped_column(String, nullable=False)
    # N-3 (KAM / self-validation) threshold in KEUR
    n3_kam_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # N-2 (Zone Manager) threshold in KEUR
    n2_zone_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # N-1 (VP Sales) threshold in KEUR
    n1_vp_limit: Mapped[int] = mapped_column(Integer, nullable=False)
