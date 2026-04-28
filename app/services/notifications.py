import datetime
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_log import NotificationLog

logger = logging.getLogger(__name__)

EMAIL_SLA_REMINDER = "SLA_REMINDER"
EMAIL_VALIDATION_REQUEST = "VALIDATION_REQUEST"
EMAIL_REVISION_REQUEST = "REVISION_REQUEST"
EMAIL_COSTING_ENTRY = "COSTING_ENTRY"
EMAIL_COSTING_RECEPTION_RESULT = "COSTING_RECEPTION_RESULT"
EMAIL_COSTING_HANDOFF = "COSTING_HANDOFF"
EMAIL_FEASIBILITY_RESULT = "FEASIBILITY_RESULT"
EMAIL_BOM_READY = "BOM_READY"
EMAIL_PRICING_READY = "PRICING_READY"
EMAIL_COSTING_APPROVED = "COSTING_APPROVED"
EMAIL_COSTING_REJECTED = "COSTING_REJECTED"
EMAIL_COSTING_MESSAGE = "COSTING_MESSAGE"


def normalize_notification_recipients(
    recipients: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    if recipients is None:
        return []
    candidates = [recipients] if isinstance(recipients, str) else list(recipients)

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        email = str(candidate or "").strip()
        if not email:
            continue
        lowered = email.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(email)
    return normalized


async def add_notification_logs(
    db: AsyncSession,
    *,
    rfq_id: str,
    recipients: str | list[str] | tuple[str, ...] | None,
    email_type: str,
    sent_at: datetime.datetime | None = None,
) -> int:
    sent_at = sent_at or datetime.datetime.utcnow()
    logs = [
        NotificationLog(
            rfq_id=rfq_id,
            recipient_email=recipient,
            email_type=email_type,
            sent_at=sent_at,
        )
        for recipient in normalize_notification_recipients(recipients)
    ]
    for log in logs:
        db.add(log)
    return len(logs)


async def record_notification_sent(
    db: AsyncSession,
    *,
    rfq_id: str,
    recipients: str | list[str] | tuple[str, ...] | None,
    email_type: str,
) -> int:
    try:
        count = await add_notification_logs(
            db,
            rfq_id=rfq_id,
            recipients=recipients,
            email_type=email_type,
        )
        if count:
            await db.commit()
        return count
    except Exception:
        await db.rollback()
        logger.exception(
            "Failed to record notification log for RFQ %s and email type %s.",
            rfq_id,
            email_type,
        )
        return 0
