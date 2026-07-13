from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.audit_log import AuditLog
from app.schemas.rfq import AuditLogOut

router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])


@router.get("", response_model=list[AuditLogOut])
async def list_audit_logs(
    rfq_id: str | None = Query(default=None),
    performed_by: str | None = Query(default=None, description="Filter by performer (case-insensitive, partial match)"),
    action: str | None = Query(default=None, description="Filter by action (case-insensitive, partial match)"),
    limit: int = Query(default=1000, le=5000),
    db: AsyncSession = Depends(get_db),
):
    query = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if rfq_id:
        query = query.where(AuditLog.rfq_id == rfq_id)
    if performed_by:
        query = query.where(AuditLog.performed_by.ilike(f"%{performed_by}%"))
    if action:
        query = query.where(AuditLog.action.ilike(f"%{action}%"))
    result = await db.execute(query)
    return result.scalars().all()
