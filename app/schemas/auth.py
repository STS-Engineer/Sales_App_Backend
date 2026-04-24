from pydantic import BaseModel, EmailStr
from typing import Optional

from app.models.user import UserRole


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    user_id: str
    email: str
    full_name: str | None = None
    role: str
    is_approved: bool = False
    region: Optional[str] = None

    model_config = {"from_attributes": True}


class ApproveUserRequest(BaseModel):
    """Owner approves a user and assigns their role."""
    role: UserRole
    region: str | None = None
