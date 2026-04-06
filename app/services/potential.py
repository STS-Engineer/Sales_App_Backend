from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.potential import Potential
from app.schemas.potential import normalize_potential_update_payload

POTENTIAL_SHARED_FIELDS: tuple[str, ...] = (
    "customer",
    "customer_location",
    "application",
    "contact_name",
    "contact_email",
    "contact_phone",
    "contact_function",
)

POTENTIAL_TO_RFQ_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("customer", "customer_name"),
    ("application", "application"),
    ("contact_name", "contact_name"),
    ("contact_email", "contact_email"),
    ("contact_phone", "contact_phone"),
    ("contact_function", "contact_role"),
)

POTENTIAL_ALLOWED_FIELDS: dict[str, str] = {
    "customer": "text",
    "customer_location": "text",
    "application": "text",
    "contact_name": "text",
    "contact_email": "text",
    "contact_phone": "text",
    "contact_function": "text",
    "industry_served": "text",
    "planned_product_type": "text",
    "engagement_reasons": "text",
    "idea_source": "text",
    "current_supplier": "text",
    "main_win_reason": "text",
    "win_rationale_details": "text",
    "technical_capabilities": "text",
    "strategic_fit": "text",
    "strategic_fit_details": "text",
    "sales_keur": "float",
    "margin_percentage": "float",
    "start_of_production": "text",
    "development_effort": "text",
    "side_effects": "text",
    "risks_to_do": "text",
    "risks_not_to_do": "text",
}


def clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_customer_name(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).casefold()


def slugify_customer_name(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value))
    ascii_text = text.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").upper()
    return slug or "UNKNOWN"


def coerce_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = clean_text(value).replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def calculate_margin_keur(sales_keur, margin_percentage) -> float | None:
    sales = coerce_float(sales_keur)
    margin = coerce_float(margin_percentage)
    if sales is None or margin is None:
        return None
    return round((sales * margin) / 100, 2)


async def assign_potential_systematic_id(
    db: AsyncSession,
    potential: Potential,
    customer_name: str | None,
) -> str | None:
    if potential.potential_systematic_id:
        return potential.potential_systematic_id

    normalized_customer = normalize_customer_name(customer_name)
    if not normalized_customer:
        return None

    count_query = await db.execute(
        select(func.count())
        .select_from(Potential)
        .where(
            func.lower(func.trim(Potential.customer)) == normalized_customer,
            Potential.rfq_id != potential.rfq_id,
        )
    )
    current_count = count_query.scalar_one() or 0
    potential.potential_systematic_id = (
        f"POT-{current_count + 1}-{slugify_customer_name(customer_name)}"
    )
    return potential.potential_systematic_id


async def update_potential_fields(
    db: AsyncSession,
    potential: Potential,
    fields_to_update: dict,
) -> tuple[dict[str, object], list[str]]:
    normalized_fields, ignored_fields = normalize_potential_update_payload(
        fields_to_update
    )
    filtered_fields: dict[str, object] = {}

    for key, value in normalized_fields.items():
        field_type = POTENTIAL_ALLOWED_FIELDS.get(key)
        if not field_type:
            ignored_fields.append(key)
            continue

        if field_type == "float":
            coerced = coerce_float(value)
            setattr(potential, key, coerced)
            filtered_fields[key] = coerced
        else:
            cleaned = clean_text(value) or None
            setattr(potential, key, cleaned)
            filtered_fields[key] = cleaned

    customer_name = clean_text(filtered_fields.get("customer") or potential.customer)
    if customer_name and not potential.potential_systematic_id:
        await assign_potential_systematic_id(db, potential, customer_name)

    potential.margin_keur = calculate_margin_keur(
        potential.sales_keur,
        potential.margin_percentage,
    )

    return filtered_fields, sorted(ignored_fields)


def get_missing_potential_shared_fields(potential: Potential | None) -> list[str]:
    if potential is None:
        return list(POTENTIAL_SHARED_FIELDS)

    missing: list[str] = []
    for field in POTENTIAL_SHARED_FIELDS:
        if not clean_text(getattr(potential, field, None)):
            missing.append(field)
    return missing


def sync_potential_to_rfq_data(
    potential: Potential,
    current_rfq_data: dict | None,
) -> dict:
    next_data = dict(current_rfq_data or {})

    for potential_field, rfq_field in POTENTIAL_TO_RFQ_FIELD_MAP:
        value = clean_text(getattr(potential, potential_field, None))
        if value:
            next_data[rfq_field] = value

    if not clean_text(next_data.get("country")):
        customer_location = clean_text(potential.customer_location)
        if customer_location:
            next_data["country"] = customer_location

    return next_data
