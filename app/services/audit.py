import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.rfq import Rfq


async def log_action(
    db: AsyncSession,
    rfq_id: str,
    action: str,
    performed_by: str,
) -> AuditLog:
    """
    Appends an audit log entry for an RFQ action.
    Does NOT commit — the caller is responsible for committing the transaction.
    """
    # db.get() hits the session's identity map first, so this is free when the
    # caller already loaded `rfq` earlier in the same request.
    rfq = await db.get(Rfq, rfq_id)
    entry = AuditLog(
        log_id=str(uuid.uuid4()),
        rfq_id=rfq_id,
        systematic_rfq_id=rfq.systematic_rfq_id if rfq else None,
        action=action,
        performed_by=performed_by,
    )
    db.add(entry)
    return entry
