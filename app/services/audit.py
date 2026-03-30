import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


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
    entry = AuditLog(
        log_id=str(uuid.uuid4()),
        rfq_id=rfq_id,
        action=action,
        performed_by=performed_by,
    )
    db.add(entry)
    return entry
