import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.security import hash_password, needs_password_rehash, verify_password


class UserRole(str, enum.Enum):
    OWNER = "OWNER"
    COMMERCIAL = "COMMERCIAL"
    ZONE_MANAGER = "ZONE_MANAGER"
    COSTING_TEAM = "COSTING_TEAM"
    RND = "RND"
    PLANT_MANAGER = "PLANT_MANAGER"
    PLM = "PLM"


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="userrole"), default=UserRole.COMMERCIAL, nullable=False
    )
    is_approved: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def set_password(self, password: str) -> None:
        self.password_hash = hash_password(password)

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.password_hash)

    def needs_password_rehash(self) -> bool:
        return needs_password_rehash(self.password_hash)
