from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import extract, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_db4
from app.middleware.auth import get_current_user
from app.models.kpi_annual_target import KpiAnnualTarget
from app.models.kpi_new_business import KpiNewBusiness
from app.models.kpi_opportunity import KpiOpportunity
from app.models.rfq import Rfq
from app.models.user import User, UserRole
from app.schemas.kpi import (
    KpiByZoneItem,
    KpiBySiteItem,
    KpiConsolidatedOut,
    KpiIndividualOut,
    KpiIndividualProductCategoryRow,
    KpiMonthlyPoint,
    KpiNewBusinessCreate,
    KpiNewBusinessKpi,
    KpiNewBusinessOut,
    KpiNewBusinessUpdate,
    KpiOpportunityCreate,
    KpiOpportunityOut,
    KpiOpportunityUpdate,
    KpiRenewalKpi,
    KpiRfqKpi,
    KpiSalespersonRow,
)

router = APIRouter(prefix="/api/kpi", tags=["kpi"])

# Recursive org-tree query reused from team_view pattern
_TEAM_EMAILS_SQL = text("""
WITH RECURSIVE team_tree AS (
    SELECT email FROM v_sales_organisation
    WHERE lower(email) = lower(:manager_email)
    UNION ALL
    SELECT child.email FROM v_sales_organisation child
    INNER JOIN team_tree parent ON lower(child.reports_to_email) = lower(parent.email)
)
SELECT email FROM team_tree
""")

_MONTH_LABELS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

PRODUCT_CATEGORIES = ["Brush & Choke", "Assembly & Seal"]


# ── Helpers ───────────────────────────────────────────────────────────

def _normalize_sector(value: str | None) -> str:
    if not value:
        return ""
    n = str(value).strip().lower().replace("-", " ").replace("_", " ")
    if "non" in n and "auto" in n:
        return "Non automotive"
    if "auto" in n:
        return "Automotive"
    return value


def _rfq_zone(data: dict) -> str:
    return (data.get("delivery_zone") or "Unknown").strip() or "Unknown"


def _rfq_sector(data: dict) -> str:
    return _normalize_sector(
        data.get("automotive_type") or data.get("automotiveType")
    )


def _monthly_points(year: int, m_map: dict[int, float]) -> list[KpiMonthlyPoint]:
    return [
        KpiMonthlyPoint(year=year, month=m, label=_MONTH_LABELS[m], value=m_map.get(m, 0.0))
        for m in range(1, 13)
    ]


# ── Opportunity CRUD ──────────────────────────────────────────────────

@router.get("/opportunities", response_model=list[KpiOpportunityOut])
async def list_opportunities(
    year: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(KpiOpportunity)
    if year is not None:
        q = q.where(KpiOpportunity.year == year)
    result = await db.execute(q.order_by(KpiOpportunity.created_at.desc()))
    return result.scalars().all()


@router.post("/opportunities", response_model=KpiOpportunityOut, status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    body: KpiOpportunityCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    opp = KpiOpportunity(**body.model_dump())
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp


@router.put("/opportunities/{opp_id}", response_model=KpiOpportunityOut)
async def update_opportunity(
    opp_id: str,
    body: KpiOpportunityUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(KpiOpportunity).where(KpiOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(opp, field, value)
    await db.commit()
    await db.refresh(opp)
    return opp


@router.delete("/opportunities/{opp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_opportunity(
    opp_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(KpiOpportunity).where(KpiOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    await db.delete(opp)
    await db.commit()


# ── New Business CRUD ─────────────────────────────────────────────────

@router.get("/new-business", response_model=list[KpiNewBusinessOut])
async def list_new_business(
    year: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(KpiNewBusiness)
    if year is not None:
        q = q.where(KpiNewBusiness.year == year)
    result = await db.execute(
        q.order_by(KpiNewBusiness.year.desc(), KpiNewBusiness.month.desc())
    )
    return result.scalars().all()


@router.post("/new-business", response_model=KpiNewBusinessOut, status_code=status.HTTP_201_CREATED)
async def create_new_business(
    body: KpiNewBusinessCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    nb = KpiNewBusiness(**body.model_dump())
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb


@router.put("/new-business/{nb_id}", response_model=KpiNewBusinessOut)
async def update_new_business(
    nb_id: str,
    body: KpiNewBusinessUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(KpiNewBusiness).where(KpiNewBusiness.id == nb_id))
    nb = result.scalar_one_or_none()
    if not nb:
        raise HTTPException(status_code=404, detail="New business entry not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(nb, field, value)
    await db.commit()
    await db.refresh(nb)
    return nb


@router.delete("/new-business/{nb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_new_business(
    nb_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(KpiNewBusiness).where(KpiNewBusiness.id == nb_id))
    nb = result.scalar_one_or_none()
    if not nb:
        raise HTTPException(status_code=404, detail="New business entry not found")
    await db.delete(nb)
    await db.commit()


# ── Consolidated dashboard ────────────────────────────────────────────

@router.get("/consolidated/{year}", response_model=KpiConsolidatedOut)
async def get_consolidated(
    year: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    db_org: AsyncSession = Depends(get_db4),
):
    # COMMERCIAL can only see their own data via /individual
    if current_user.role == UserRole.COMMERCIAL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Use /individual endpoint for your own KPIs",
        )

    # ZONE_MANAGER: build the set of allowed emails (self + team)
    allowed_emails: set[str] | None = None
    if current_user.role == UserRole.ZONE_MANAGER:
        rows = (
            await db_org.execute(_TEAM_EMAILS_SQL, {"manager_email": current_user.email})
        ).fetchall()
        allowed_emails = {r[0].lower() for r in rows}
        allowed_emails.add(current_user.email.lower())

    # Load all data
    settings = (
        await db.execute(select(KpiAnnualTarget).where(KpiAnnualTarget.year == year))
    ).scalar_one_or_none()

    opps = (
        await db.execute(select(KpiOpportunity).where(KpiOpportunity.year == year))
    ).scalars().all()

    nbs = (
        await db.execute(select(KpiNewBusiness).where(KpiNewBusiness.year == year))
    ).scalars().all()

    rfq_rows = (
        await db.execute(
            select(Rfq.rfq_data, Rfq.created_at, Rfq.created_by_email).where(
                extract("year", Rfq.created_at) == year
            )
        )
    ).all()

    # ZONE_MANAGER: restrict all data to team scope
    if allowed_emails is not None:
        opps = [o for o in opps if (o.salesperson_email or "").lower() in allowed_emails]
        nbs = [nb for nb in nbs if (nb.salesperson_email or "").lower() in allowed_emails]
        rfq_rows = [r for r in rfq_rows if (r.created_by_email or "").lower() in allowed_emails]

    # Unpack settings
    renewal_pct = settings.renewal_pct if settings else 25.0
    total_ca = settings.total_ca_meur if settings else None
    excluded_raw = (settings.excluded_zones or []) if settings else []
    excluded_total = sum(float(z.get("ca_meur", 0)) for z in excluded_raw)
    outstanding = (total_ca - excluded_total) if total_ca is not None else None
    renewal_annual = (outstanding * 1000 * renewal_pct / 100) if outstanding is not None else None
    renewal_monthly = (renewal_annual / 12) if renewal_annual is not None else None
    rfq_auto_target = settings.rfq_automotive_monthly_target if settings else 40
    rfq_non_auto_target = settings.rfq_non_auto_monthly_target if settings else 8
    nb_monthly_target = settings.new_business_monthly_keur if settings else 2000.0
    sp_targets = (settings.salesperson_targets or []) if settings else []
    sites_cfg = (settings.sites or []) if settings else []

    # ── Renewal ───────────────────────────────────────────────────────
    confirmed_keur = sum(o.annual_keur for o in opps if o.probability >= 100.0)
    pipeline_total = sum(o.annual_keur for o in opps)
    pipeline_weighted = sum(o.annual_keur * o.probability / 100.0 for o in opps)

    site_map: dict[str, dict[str, float]] = {
        s.get("name", ""): {"confirmed": 0.0, "total": 0.0}
        for s in sites_cfg if s.get("name")
    }
    for o in opps:
        key = (o.site or "Unassigned").strip() or "Unassigned"
        if key not in site_map:
            site_map[key] = {"confirmed": 0.0, "total": 0.0}
        site_map[key]["total"] += o.annual_keur
        if o.probability >= 100.0:
            site_map[key]["confirmed"] += o.annual_keur

    renewal_monthly_map: dict[int, float] = {}
    for o in opps:
        if o.created_at and o.probability >= 100.0:
            m = o.created_at.month
            renewal_monthly_map[m] = renewal_monthly_map.get(m, 0.0) + o.annual_keur

    renewal_kpi = KpiRenewalKpi(
        confirmed_keur=confirmed_keur,
        annual_target_keur=renewal_annual,
        monthly_target_keur=renewal_monthly,
        pipeline_total_keur=pipeline_total,
        pipeline_weighted_keur=pipeline_weighted,
        by_site=[
            KpiBySiteItem(site=k, confirmed_keur=v["confirmed"], total_keur=v["total"])
            for k, v in sorted(site_map.items()) if k
        ],
        monthly=_monthly_points(year, renewal_monthly_map),
    )

    # ── New Business ──────────────────────────────────────────────────
    nb_monthly_map: dict[int, float] = {}
    nb_zone_map: dict[str, float] = {}
    for nb in nbs:
        nb_monthly_map[nb.month] = nb_monthly_map.get(nb.month, 0.0) + nb.annual_keur
        z = (nb.zone or "Unknown").strip() or "Unknown"
        nb_zone_map[z] = nb_zone_map.get(z, 0.0) + nb.annual_keur

    nb_kpi = KpiNewBusinessKpi(
        ytd_keur=sum(nb.annual_keur for nb in nbs),
        monthly_target_keur=nb_monthly_target,
        monthly=_monthly_points(year, nb_monthly_map),
        by_zone=[KpiByZoneItem(zone=k, value=v) for k, v in sorted(nb_zone_map.items())],
    )

    # ── RFQ KPIs ──────────────────────────────────────────────────────
    auto_m: dict[int, float] = {}
    non_auto_m: dict[int, float] = {}
    auto_z: dict[str, float] = {}
    non_auto_z: dict[str, float] = {}

    for row in rfq_rows:
        data = row.rfq_data or {}
        sector = _rfq_sector(data)
        if not row.created_at:
            continue
        m = row.created_at.month
        z = _rfq_zone(data)
        if sector == "Automotive":
            auto_m[m] = auto_m.get(m, 0.0) + 1
            auto_z[z] = auto_z.get(z, 0.0) + 1
        elif sector == "Non automotive":
            non_auto_m[m] = non_auto_m.get(m, 0.0) + 1
            non_auto_z[z] = non_auto_z.get(z, 0.0) + 1

    rfq_auto_kpi = KpiRfqKpi(
        monthly_target=rfq_auto_target,
        monthly=_monthly_points(year, auto_m),
        by_zone=[KpiByZoneItem(zone=k, value=v) for k, v in sorted(auto_z.items())],
    )
    rfq_non_auto_kpi = KpiRfqKpi(
        monthly_target=rfq_non_auto_target,
        monthly=_monthly_points(year, non_auto_m),
        by_zone=[KpiByZoneItem(zone=k, value=v) for k, v in sorted(non_auto_z.items())],
    )

    # ── Salesperson rows ──────────────────────────────────────────────
    sp_rows: list[KpiSalespersonRow] = []
    for sp in sp_targets:
        ident = sp.get("identifier", "")
        # Filter to team scope for ZONE_MANAGER
        if allowed_emails is not None and ident.lower() not in allowed_emails:
            continue
        label = sp.get("label", ident)
        target_keur = float(sp.get("annual_keur", 0))
        ident_lc = ident.lower()

        sp_confirmed = sum(
            o.annual_keur for o in opps
            if (o.salesperson_email or "").lower() == ident_lc and o.probability >= 100.0
        )
        sp_nb = sum(
            nb.annual_keur for nb in nbs
            if (nb.salesperson_email or "").lower() == ident_lc
        )
        sp_rfq = sum(
            1 for r in rfq_rows
            if (r.created_by_email or "").lower() == ident_lc
        )
        pct_renewal = (sp_confirmed / target_keur * 100) if target_keur else None
        nb_yearly = nb_monthly_target * 12
        pct_nb = (sp_nb / nb_yearly * 100) if nb_yearly else 0.0

        sp_rows.append(
            KpiSalespersonRow(
                identifier=ident,
                label=label,
                renewal_confirmed_keur=sp_confirmed,
                renewal_target_keur=target_keur or None,
                new_business_keur=sp_nb,
                new_business_monthly_target_keur=nb_monthly_target,
                nb_rfq=sp_rfq,
                pct_renewal=pct_renewal,
                pct_new_business=pct_nb,
            )
        )

    return KpiConsolidatedOut(
        year=year,
        renewal=renewal_kpi,
        new_business=nb_kpi,
        rfq_automotive=rfq_auto_kpi,
        rfq_non_auto=rfq_non_auto_kpi,
        salespersons=sp_rows,
    )


# ── Individual dashboard ──────────────────────────────────────────────

@router.get("/individual/{email}/{year}", response_model=KpiIndividualOut)
async def get_individual(
    email: str,
    year: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    db_org: AsyncSession = Depends(get_db4),
):
    email_lc = email.lower()

    # Access control
    if current_user.role == UserRole.COMMERCIAL:
        if email_lc != current_user.email.lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own KPIs")
    elif current_user.role == UserRole.ZONE_MANAGER:
        if email_lc != current_user.email.lower():
            rows = (
                await db_org.execute(_TEAM_EMAILS_SQL, {"manager_email": current_user.email})
            ).fetchall()
            team_emails = {r[0].lower() for r in rows}
            if email_lc not in team_emails:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your team's KPIs")
    # OWNER: no restriction

    settings = (
        await db.execute(select(KpiAnnualTarget).where(KpiAnnualTarget.year == year))
    ).scalar_one_or_none()

    sp_targets = (settings.salesperson_targets or []) if settings else []
    nb_monthly_target = settings.new_business_monthly_keur if settings else 2000.0

    sp_cfg = next(
        (sp for sp in sp_targets if sp.get("identifier", "").lower() == email_lc), None
    )
    annual_keur = float(sp_cfg.get("annual_keur", 0)) if sp_cfg else None
    label = sp_cfg.get("label", email) if sp_cfg else email

    opps = (
        await db.execute(
            select(KpiOpportunity).where(
                KpiOpportunity.year == year,
                func.lower(KpiOpportunity.salesperson_email) == email_lc,
            )
        )
    ).scalars().all()

    nbs = (
        await db.execute(
            select(KpiNewBusiness).where(
                KpiNewBusiness.year == year,
                func.lower(KpiNewBusiness.salesperson_email) == email_lc,
            )
        )
    ).scalars().all()

    nb_rfq = (
        await db.execute(
            select(func.count(Rfq.rfq_id)).where(
                extract("year", Rfq.created_at) == year,
                func.lower(Rfq.created_by_email) == email_lc,
            )
        )
    ).scalar_one() or 0

    renewal_confirmed = sum(o.annual_keur for o in opps if o.probability >= 100.0)
    renewal_pipeline = sum(o.annual_keur for o in opps)
    nb_ytd = sum(nb.annual_keur for nb in nbs)

    category_rows: list[KpiIndividualProductCategoryRow] = []
    for cat in PRODUCT_CATEGORIES:
        cat_nbs = [nb for nb in nbs if nb.product_category == cat]
        m_map: dict[int, float] = {}
        for nb in cat_nbs:
            m_map[nb.month] = m_map.get(nb.month, 0.0) + nb.annual_keur
        category_rows.append(
            KpiIndividualProductCategoryRow(
                category=cat,
                monthly=_monthly_points(year, m_map),
                ytd_keur=sum(nb.annual_keur for nb in cat_nbs),
                deals=[KpiNewBusinessOut.model_validate(nb) for nb in cat_nbs],
            )
        )

    return KpiIndividualOut(
        year=year,
        salesperson_email=email,
        label=label,
        annual_target_keur=annual_keur,
        renewal_portfolio=[KpiOpportunityOut.model_validate(o) for o in opps],
        renewal_confirmed_keur=renewal_confirmed,
        renewal_pipeline_keur=renewal_pipeline,
        new_business_categories=category_rows,
        new_business_ytd_keur=nb_ytd,
        nb_rfq=nb_rfq,
    )
