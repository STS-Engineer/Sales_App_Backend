from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.models.user import UserRole


class UserOut(BaseModel):
    user_id: str
    email: EmailStr
    full_name: str | None = None
    role: UserRole
    roles: list[str] = []
    is_approved: bool = False
    region: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleUpdateRequest(BaseModel):
    role: UserRole
    roles: list[UserRole] | None = None
    is_approved: bool | None = None
