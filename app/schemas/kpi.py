from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


# ── Settings ──────────────────────────────────────────────────────────

class ExcludedZoneItem(BaseModel):
    name: str
    ca_meur: float = 0.0


class SiteItem(BaseModel):
    name: str
    ca_meur: float = 0.0


class SalespersonTargetItem(BaseModel):
    identifier: str
    label: str
    annual_keur: float = 0.0


class KpiAnnualTargetOut(BaseModel):
    id: str
    year: int
    total_ca_meur: Optional[float] = None
    renewal_pct: float = 25.0
    rfq_automotive_monthly_target: int = 40
    rfq_non_auto_monthly_target: int = 8
    new_business_monthly_keur: float = 2000.0
    excluded_zones: list[ExcludedZoneItem] = []
    sites: list[SiteItem] = []
    salesperson_targets: list[SalespersonTargetItem] = []
    outstanding_meur: Optional[float] = None
    renewal_annual_keur: Optional[float] = None

    model_config = {"from_attributes": True}


class KpiAnnualTargetUpsert(BaseModel):
    total_ca_meur: Optional[float] = None
    renewal_pct: float = 25.0
    rfq_automotive_monthly_target: int = 40
    rfq_non_auto_monthly_target: int = 8
    new_business_monthly_keur: float = 2000.0
    excluded_zones: list[ExcludedZoneItem] = []
    sites: list[SiteItem] = []
    salesperson_targets: list[SalespersonTargetItem] = []


# ── Opportunity ───────────────────────────────────────────────────────

class KpiOpportunityOut(BaseModel):
    id: str
    year: int
    customer: str
    product_line: Optional[str] = None
    site: Optional[str] = None
    zone: Optional[str] = None
    salesperson_email: Optional[str] = None
    annual_keur: float = 0.0
    probability: float = 0.0
    sector: Optional[str] = None
    description: Optional[str] = None

    model_config = {"from_attributes": True}


class KpiOpportunityCreate(BaseModel):
    year: int
    customer: str
    product_line: Optional[str] = None
    site: Optional[str] = None
    zone: Optional[str] = None
    salesperson_email: Optional[str] = None
    annual_keur: float = 0.0
    probability: float = 0.0
    sector: Optional[str] = None
    description: Optional[str] = None


class KpiOpportunityUpdate(BaseModel):
    customer: Optional[str] = None
    product_line: Optional[str] = None
    site: Optional[str] = None
    zone: Optional[str] = None
    salesperson_email: Optional[str] = None
    annual_keur: Optional[float] = None
    probability: Optional[float] = None
    sector: Optional[str] = None
    description: Optional[str] = None


# ── New Business ──────────────────────────────────────────────────────

class KpiNewBusinessOut(BaseModel):
    id: str
    year: int
    month: int
    customer: str
    project_name: Optional[str] = None
    product_category: Optional[str] = None
    product_line: Optional[str] = None
    zone: Optional[str] = None
    site: Optional[str] = None
    salesperson_email: Optional[str] = None
    annual_keur: float = 0.0
    sop: Optional[str] = None
    sector: Optional[str] = None

    model_config = {"from_attributes": True}


class KpiNewBusinessCreate(BaseModel):
    year: int
    month: int
    customer: str
    project_name: Optional[str] = None
    product_category: Optional[str] = None
    product_line: Optional[str] = None
    zone: Optional[str] = None
    site: Optional[str] = None
    salesperson_email: Optional[str] = None
    annual_keur: float = 0.0
    sop: Optional[str] = None
    sector: Optional[str] = None


class KpiNewBusinessUpdate(BaseModel):
    customer: Optional[str] = None
    project_name: Optional[str] = None
    product_category: Optional[str] = None
    product_line: Optional[str] = None
    zone: Optional[str] = None
    site: Optional[str] = None
    salesperson_email: Optional[str] = None
    annual_keur: Optional[float] = None
    sop: Optional[str] = None
    sector: Optional[str] = None


# ── Dashboard ─────────────────────────────────────────────────────────

class KpiMonthlyPoint(BaseModel):
    year: int
    month: int
    label: str
    value: float


class KpiByZoneItem(BaseModel):
    zone: str
    value: float


class KpiBySiteItem(BaseModel):
    site: str
    confirmed_keur: float
    total_keur: float


class KpiRenewalKpi(BaseModel):
    confirmed_keur: float
    annual_target_keur: Optional[float]
    monthly_target_keur: Optional[float]
    pipeline_total_keur: float
    pipeline_weighted_keur: float
    by_site: list[KpiBySiteItem]
    monthly: list[KpiMonthlyPoint]


class KpiNewBusinessKpi(BaseModel):
    ytd_keur: float
    monthly_target_keur: float
    monthly: list[KpiMonthlyPoint]
    by_zone: list[KpiByZoneItem]


class KpiRfqKpi(BaseModel):
    monthly_target: int
    monthly: list[KpiMonthlyPoint]
    by_zone: list[KpiByZoneItem]


class KpiSalespersonRow(BaseModel):
    identifier: str
    label: str
    renewal_confirmed_keur: float
    renewal_target_keur: Optional[float]
    new_business_keur: float
    new_business_monthly_target_keur: float
    nb_rfq: int
    pct_renewal: Optional[float]
    pct_new_business: float


class KpiConsolidatedOut(BaseModel):
    year: int
    renewal: KpiRenewalKpi
    new_business: KpiNewBusinessKpi
    rfq_automotive: KpiRfqKpi
    rfq_non_auto: KpiRfqKpi
    salespersons: list[KpiSalespersonRow]


class KpiIndividualProductCategoryRow(BaseModel):
    category: str
    monthly: list[KpiMonthlyPoint]
    ytd_keur: float
    deals: list[KpiNewBusinessOut]


class KpiIndividualOut(BaseModel):
    year: int
    salesperson_email: str
    label: str
    annual_target_keur: Optional[float]
    renewal_portfolio: list[KpiOpportunityOut]
    renewal_confirmed_keur: float
    renewal_pipeline_keur: float
    new_business_categories: list[KpiIndividualProductCategoryRow]
    new_business_ytd_keur: float
    nb_rfq: int
