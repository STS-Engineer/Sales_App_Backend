from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Contact(Base):
    __tablename__ = "contacts"

    contact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contact_email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    contact_name: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_function: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
