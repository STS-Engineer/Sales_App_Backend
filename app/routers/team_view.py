import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db, get_db4
from app.middleware.auth import require_role
from app.models.rfq import Rfq
from app.models.user import User, UserRole
from app.schemas.rfq import RfqOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/team-view", tags=["team-view"])

_TEAM_EMAILS_SQL = text("""
WITH RECURSIVE team_tree AS (
    SELECT email
    FROM v_sales_organisation
    WHERE lower(email) = lower(:current_user_email)

    UNION ALL

    SELECT child.email
    FROM v_sales_organisation child
    INNER JOIN team_tree parent
        ON lower(child.reports_to_email) = lower(parent.email)
)
SELECT email
FROM team_tree
WHERE lower(email) <> lower(:current_user_email)
""")

_TEAM_MEMBERS_SQL = text("""
WITH RECURSIVE team_tree AS (
    SELECT person, email
    FROM v_sales_organisation
    WHERE lower(email) = lower(:current_user_email)

    UNION ALL

    SELECT child.person, child.email
    FROM v_sales_organisation child
    INNER JOIN team_tree parent
        ON lower(child.reports_to_email) = lower(parent.email)
)
SELECT DISTINCT person, email
FROM team_tree
WHERE lower(email) <> lower(:current_user_email)
  AND person IS NOT NULL
ORDER BY person
""")


_SELF_SQL = text("""
SELECT person, email
FROM v_sales_organisation
WHERE lower(email) = lower(:email)
LIMIT 1
""")


@router.get("/members")
async def get_team_members(
    include_self: bool = Query(default=False),
    current_user: User = Depends(require_role(UserRole.ZONE_MANAGER)),
    db_kpi: AsyncSession = Depends(get_db4),
):
    try:
        result = await db_kpi.execute(
            _TEAM_MEMBERS_SQL,
            {"current_user_email": current_user.email},
        )
        rows = result.mappings().all()
        members = [{"person": row.person, "email": row.email} for row in rows]

        if include_self:
            self_result = await db_kpi.execute(_SELF_SQL, {"email": current_user.email})
            self_row = self_result.mappings().first()
            self_entry = {
                "person": self_row.person if self_row else (current_user.full_name or current_user.email),
                "email": current_user.email,
            }
            members = [self_entry] + members

        return members
    except Exception:
        logger.exception(
            "Failed to query team members for user %s", current_user.email
        )
        return []


@router.get("", response_model=list[RfqOut])
async def get_team_view(
    current_user: User = Depends(require_role(UserRole.ZONE_MANAGER)),
    db_kpi: AsyncSession = Depends(get_db4),
    db_main: AsyncSession = Depends(get_db),
):
    try:
        email_result = await db_kpi.execute(
            _TEAM_EMAILS_SQL,
            {"current_user_email": current_user.email},
        )
        team_emails = [row.email for row in email_result.mappings().all() if row.email]
    except Exception:
        logger.exception(
            "Failed to query v_sales_organisation for user %s", current_user.email
        )
        return []

    if not team_emails:
        return []

    rfq_result = await db_main.execute(
        select(Rfq)
        .options(selectinload(Rfq.potential), selectinload(Rfq.offer_preparation))
        .where(Rfq.created_by_email.in_(team_emails))
        .order_by(Rfq.created_at.desc())
    )
    return rfq_result.scalars().all()
