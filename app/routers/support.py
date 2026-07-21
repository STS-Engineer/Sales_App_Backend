import logging

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.support import SupportTicketRequest, SupportTicketResponse
from app.utils import emails

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/support", tags=["support"])


@router.post("/tickets", response_model=SupportTicketResponse)
async def submit_support_ticket(
    body: SupportTicketRequest,
    current_user: User = Depends(get_current_user),
):
    recipients = settings.support_ticket_recipients
    if not recipients:
        raise HTTPException(
            status_code=503,
            detail="Support ticket recipients are not configured.",
        )

    sent = emails.send_support_ticket_email(
        recipients,
        current_user.email,
        current_user.full_name,
        body.subject.strip(),
        body.description.strip(),
    )
    if not sent:
        logger.error("Failed to send support ticket email from %s", current_user.email)
        raise HTTPException(status_code=502, detail="Unable to send the report. Please try again.")

    return SupportTicketResponse(sent=True)
