from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


POTENTIAL_UPDATE_ALIASES: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "customer_name", "customerName"),
    "customer_location": (
        "customer_location",
        "customerLocation",
        "potentialCustomerLocation",
    ),
    "application": ("application",),
    "contact_name": ("contact_name", "contactName"),
    "contact_email": ("contact_email", "contactEmail"),
    "contact_phone": ("contact_phone", "contactPhone"),
    "contact_function": (
        "contact_function",
        "contactFunction",
        "contact_role",
        "contactRole",
    ),
    "industry_served": (
        "industry_served",
        "industryServed",
        "potentialIndustry",
    ),
    "planned_product_type": (
        "planned_product_type",
        "plannedProductType",
        "potentialProductType",
    ),
    "engagement_reasons": (
        "engagement_reasons",
        "engagementReasons",
        "potentialEngagementReason",
    ),
    "idea_source": ("idea_source", "ideaSource", "potentialIdeaOwner"),
    "current_supplier": (
        "current_supplier",
        "currentSupplier",
        "potentialCurrentSupplier",
    ),
    "main_win_reason": (
        "main_win_reason",
        "mainWinReason",
        "potentialWinReason",
    ),
    "win_rationale_details": (
        "win_rationale_details",
        "winRationaleDetails",
        "potentialWinDetails",
    ),
    "technical_capabilities": (
        "technical_capabilities",
        "technicalCapabilities",
        "potentialTechnicalCapability",
    ),
    "strategic_fit": ("strategic_fit", "strategicFit", "potentialStrategyFit"),
    "strategic_fit_details": (
        "strategic_fit_details",
        "strategicFitDetails",
        "potentialStrategyFitDetails",
    ),
    "sales_keur": ("sales_keur", "salesKeur", "potentialBusinessSalesKeur"),
    "margin_percentage": (
        "margin_percentage",
        "marginPercentage",
        "potentialBusinessMarginPercent",
    ),
    "start_of_production": (
        "start_of_production",
        "startOfProduction",
        "potentialStartOfProduction",
    ),
    "development_effort": (
        "development_effort",
        "developmentEffort",
        "potentialDevelopmentEffort",
    ),
    "side_effects": ("side_effects", "sideEffects", "potentialSideEffects"),
    "risks_to_do": ("risks_to_do", "risksToDo", "potentialRiskDoAssessment"),
    "risks_not_to_do": (
        "risks_not_to_do",
        "risksNotToDo",
        "potentialRiskNotDoAssessment",
    ),
}

POTENTIAL_UPDATE_KEY_MAP: dict[str, str] = {
    alias: field_name
    for field_name, aliases in POTENTIAL_UPDATE_ALIASES.items()
    for alias in aliases
}


class PotentialUpdate(BaseModel):
    customer: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["customer"]),
    )
    customer_location: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["customer_location"]
        ),
    )
    application: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["application"]),
    )
    contact_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["contact_name"]),
    )
    contact_email: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["contact_email"]),
    )
    contact_phone: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["contact_phone"]),
    )
    contact_function: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["contact_function"]
        ),
    )
    industry_served: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["industry_served"]
        ),
    )
    planned_product_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["planned_product_type"]
        ),
    )
    engagement_reasons: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["engagement_reasons"]
        ),
    )
    idea_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["idea_source"]),
    )
    current_supplier: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["current_supplier"]
        ),
    )
    main_win_reason: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["main_win_reason"]
        ),
    )
    win_rationale_details: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["win_rationale_details"]
        ),
    )
    technical_capabilities: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["technical_capabilities"]
        ),
    )
    strategic_fit: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["strategic_fit"]),
    )
    strategic_fit_details: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["strategic_fit_details"]
        ),
    )
    sales_keur: float | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["sales_keur"]),
    )
    margin_percentage: float | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["margin_percentage"]
        ),
    )
    start_of_production: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["start_of_production"]
        ),
    )
    development_effort: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["development_effort"]
        ),
    )
    side_effects: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["side_effects"]),
    )
    risks_to_do: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*POTENTIAL_UPDATE_ALIASES["risks_to_do"]),
    )
    risks_not_to_do: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            *POTENTIAL_UPDATE_ALIASES["risks_not_to_do"]
        ),
    )

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class PotentialOut(BaseModel):
    rfq_id: str
    potential_systematic_id: str | None
    customer: str | None
    customer_location: str | None
    application: str | None
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    contact_function: str | None
    industry_served: str | None
    planned_product_type: str | None
    engagement_reasons: str | None
    idea_source: str | None
    current_supplier: str | None
    main_win_reason: str | None
    win_rationale_details: str | None
    technical_capabilities: str | None
    strategic_fit: str | None
    strategic_fit_details: str | None
    sales_keur: float | None
    margin_percentage: float | None
    margin_keur: float | None
    start_of_production: str | None
    development_effort: str | None
    side_effects: str | None
    risks_to_do: str | None
    risks_not_to_do: str | None
    chat_history: list[dict] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


def normalize_potential_update_payload(
    payload: dict[str, object] | None,
) -> tuple[dict[str, object], list[str]]:
    remapped_payload: dict[str, object] = {}
    ignored_fields: list[str] = []

    for raw_key, value in (payload or {}).items():
        canonical_key = POTENTIAL_UPDATE_KEY_MAP.get(raw_key)
        if not canonical_key:
            ignored_fields.append(raw_key)
            continue
        remapped_payload[canonical_key] = value

    normalized = PotentialUpdate.model_validate(remapped_payload).model_dump(
        exclude_unset=True
    )
    return normalized, sorted(set(ignored_fields))
