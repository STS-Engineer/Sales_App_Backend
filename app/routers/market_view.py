import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db, get_db4
from app.middleware.auth import get_current_user
from app.models.rfq import Rfq
from app.models.user import User
from app.schemas.rfq import RfqOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-view", tags=["market-view"])

_SORT_PATH_SQL = text("""
SELECT sort_path
FROM v_sales_organisation
WHERE lower(email) = lower(:email)
LIMIT 1
""")

_LARGE_ACCOUNT_KEYWORDS = ("nidec", "valeo", "bosch")


def _resolve_segment(sort_path: str | None) -> str | None:
    if not sort_path:
        return None
    normalized = str(sort_path).strip().lower()
    if "large_account" in normalized:
        return "large_accounts"
    if "automotive" in normalized:
        return "automotive"
    if "industry" in normalized:
        return "industry"
    return None


def _normalize_sector(value: str | None) -> str:
    if not value:
        return ""
    n = str(value).strip().lower().replace("-", " ").replace("_", " ")
    if "non" in n and "auto" in n:
        return "Non automotive"
    if "auto" in n:
        return "Automotive"
    return str(value)


def _rfq_sector(rfq: Rfq) -> str:
    data = rfq.rfq_data or {}
    return _normalize_sector(data.get("automotive_type") or data.get("automotiveType"))


def _rfq_customer_name(rfq: Rfq) -> str:
    data = rfq.rfq_data or {}
    for key in ("customer_name", "customer", "client", "customerName"):
        value = data.get(key)
        if value and str(value).strip():
            return str(value).strip()
    if rfq.potential is not None and rfq.potential.customer:
        return str(rfq.potential.customer).strip()
    return ""


async def _get_current_segment(db_kpi: AsyncSession, email: str) -> str | None:
    try:
        result = await db_kpi.execute(_SORT_PATH_SQL, {"email": email})
        row = result.mappings().first()
    except Exception:
        logger.exception("Failed to query v_sales_organisation.sort_path for user %s", email)
        return None
    if not row:
        return None
    return _resolve_segment(row.get("sort_path"))


@router.get("/segment")
async def get_market_view_segment(
    current_user: User = Depends(get_current_user),
    db_kpi: AsyncSession = Depends(get_db4),
):
    segment = await _get_current_segment(db_kpi, current_user.email)
    return {"segment": segment}


@router.get("", response_model=list[RfqOut])
async def get_market_view(
    current_user: User = Depends(get_current_user),
    db_kpi: AsyncSession = Depends(get_db4),
    db_main: AsyncSession = Depends(get_db),
):
    segment = await _get_current_segment(db_kpi, current_user.email)
    if segment is None:
        return []

    result = await db_main.execute(
        select(Rfq)
        .options(selectinload(Rfq.potential), selectinload(Rfq.offer_preparation))
        .order_by(Rfq.updated_at.desc(), Rfq.created_at.desc())
    )
    rfqs = result.scalars().all()

    if segment == "automotive":
        rfqs = [rfq for rfq in rfqs if _rfq_sector(rfq) == "Automotive"]
    elif segment == "industry":
        rfqs = [rfq for rfq in rfqs if _rfq_sector(rfq) != "Automotive"]
    elif segment == "large_accounts":
        rfqs = [
            rfq
            for rfq in rfqs
            if any(keyword in _rfq_customer_name(rfq).lower() for keyword in _LARGE_ACCOUNT_KEYWORDS)
        ]
    else:
        return []

    unique_emails = {
        e
        for rfq in rfqs
        for e in (rfq.created_by_email, rfq.zone_manager_email)
        if e
    }
    name_by_email: dict[str, str] = {}
    if unique_emails:
        users_result = await db_main.execute(select(User).where(User.email.in_(unique_emails)))
        for user in users_result.scalars().all():
            if user.full_name:
                name_by_email[user.email] = user.full_name

    for rfq in rfqs:
        rfq.created_by_name = name_by_email.get(rfq.created_by_email)
        rfq.zone_manager_name = name_by_email.get(rfq.zone_manager_email or "")

    return rfqs
