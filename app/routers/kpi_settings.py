from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.kpi_annual_target import KpiAnnualTarget
from app.models.user import User, UserRole
from app.schemas.kpi import (
    ExcludedZoneItem,
    KpiAnnualTargetOut,
    KpiAnnualTargetUpsert,
    SalespersonTargetItem,
    SiteItem,
)

router = APIRouter(prefix="/api/kpi", tags=["kpi"])


def _build_target_out(target: KpiAnnualTarget) -> KpiAnnualTargetOut:
    excluded = target.excluded_zones or []
    excluded_total_meur = sum(float(z.get("ca_meur", 0)) for z in excluded)
    total = target.total_ca_meur
    outstanding_meur = (total - excluded_total_meur) if total is not None else None
    renewal_annual_keur = (
        outstanding_meur * 1000 * target.renewal_pct / 100
        if outstanding_meur is not None
        else None
    )
    return KpiAnnualTargetOut(
        id=target.id,
        year=target.year,
        total_ca_meur=total,
        renewal_pct=target.renewal_pct,
        rfq_automotive_monthly_target=target.rfq_automotive_monthly_target,
        rfq_non_auto_monthly_target=target.rfq_non_auto_monthly_target,
        new_business_monthly_keur=target.new_business_monthly_keur,
        excluded_zones=[ExcludedZoneItem(**z) for z in excluded],
        sites=[SiteItem(**s) for s in (target.sites or [])],
        salesperson_targets=[
            SalespersonTargetItem(**sp) for sp in (target.salesperson_targets or [])
        ],
        outstanding_meur=outstanding_meur,
        renewal_annual_keur=renewal_annual_keur,
    )


@router.get("/settings/{year}", response_model=KpiAnnualTargetOut)
async def get_kpi_settings(
    year: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(KpiAnnualTarget).where(KpiAnnualTarget.year == year)
    )
    target = result.scalar_one_or_none()
    if not target:
        return KpiAnnualTargetOut(id="", year=year)
    return _build_target_out(target)


@router.put("/settings/{year}", response_model=KpiAnnualTargetOut)
async def upsert_kpi_settings(
    year: int,
    body: KpiAnnualTargetUpsert,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(
        select(KpiAnnualTarget).where(KpiAnnualTarget.year == year)
    )
    target = result.scalar_one_or_none()
    if not target:
        target = KpiAnnualTarget(year=year)
        db.add(target)

    target.total_ca_meur = body.total_ca_meur
    target.renewal_pct = body.renewal_pct
    target.rfq_automotive_monthly_target = body.rfq_automotive_monthly_target
    target.rfq_non_auto_monthly_target = body.rfq_non_auto_monthly_target
    target.new_business_monthly_keur = body.new_business_monthly_keur
    target.excluded_zones = [z.model_dump() for z in body.excluded_zones]
    target.sites = [s.model_dump() for s in body.sites]
    target.salesperson_targets = [sp.model_dump() for sp in body.salesperson_targets]

    await db.commit()
    await db.refresh(target)
    return _build_target_out(target)
