from datetime import datetime

from pydantic import BaseModel, field_validator

from app.models.rfq import RfqSubStatus
from app.models.user import UserRole


class DiscussionMessageCreateRequest(BaseModel):
    phase: RfqSubStatus
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required")
        return text


class CostingMessageCreateRequest(BaseModel):
    message: str
    recipient_email: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required")
        return text

    @field_validator("recipient_email")
    @classmethod
    def validate_recipient_email(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("recipient_email is required")
        return text


class DiscussionMessageOut(BaseModel):
    id: str
    rfq_id: str
    phase: RfqSubStatus
    message: str
    recipient_email: str | None = None
    created_at: datetime
    user_id: str
    author_name: str | None = None
    author_email: str
    author_role: UserRole
