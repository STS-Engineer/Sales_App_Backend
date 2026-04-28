from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.tasks.followups import run_followup_sweep

router = APIRouter(prefix="/api/internal", tags=["internal"])


@router.post("/trigger-followups")
async def trigger_followups(
    x_cron_token: str | None = Header(default=None, alias="X-Cron-Token"),
    db: AsyncSession = Depends(get_db),
):
    configured_token = str(settings.CRON_TOKEN or "").strip()
    if not configured_token:
        raise HTTPException(status_code=503, detail="CRON_TOKEN is not configured.")
    if str(x_cron_token or "").strip() != configured_token:
        raise HTTPException(status_code=403, detail="Invalid cron token.")
    return await run_followup_sweep(db)
