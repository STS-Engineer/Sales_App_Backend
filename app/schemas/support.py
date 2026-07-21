from pydantic import BaseModel, Field


class SupportTicketRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)


class SupportTicketResponse(BaseModel):
    sent: bool
