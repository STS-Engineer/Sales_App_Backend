from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.notification_log import NotificationLog
from app.models.rfq import Rfq
from app.schemas.rfq import NotificationLogOut

router = APIRouter(prefix="/api/notification-logs", tags=["notification-logs"])


@router.get("", response_model=list[NotificationLogOut])
async def list_notification_logs(
    rfq_id: str | None = Query(default=None),
    recipient_email: str | None = Query(default=None, description="Filter by recipient (case-insensitive, partial match)"),
    email_type: str | None = Query(default=None, description="Filter by email type (case-insensitive, partial match)"),
    limit: int = Query(default=1000, le=5000),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(NotificationLog, Rfq.systematic_rfq_id)
        .outerjoin(Rfq, Rfq.rfq_id == NotificationLog.rfq_id)
        .order_by(NotificationLog.sent_at.desc(), NotificationLog.log_id.desc())
        .limit(limit)
    )
    if rfq_id:
        query = query.where(NotificationLog.rfq_id == rfq_id)
    if recipient_email:
        query = query.where(NotificationLog.recipient_email.ilike(f"%{recipient_email}%"))
    if email_type:
        query = query.where(NotificationLog.email_type.ilike(f"%{email_type}%"))

    result = await db.execute(query)
    return [
        NotificationLogOut(
            log_id=log.log_id,
            rfq_id=log.rfq_id,
            systematic_rfq_id=systematic_rfq_id,
            recipient_email=log.recipient_email,
            email_type=log.email_type,
            sent_at=log.sent_at,
        )
        for log, systematic_rfq_id in result.all()
    ]
