"""add potential table and backfill legacy potential data

Revision ID: f3c1b7a9d2e4
Revises: e2b4c6d8f0a1
Create Date: 2026-04-06 12:30:00.000000
"""

from __future__ import annotations

import re
import unicodedata

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "f3c1b7a9d2e4"
down_revision = "e2b4c6d8f0a1"
branch_labels = None
depends_on = None


def _clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _pick_first(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = _clean_text(value).replace(" ", "")
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


def _calculate_margin_keur(sales_keur, margin_percentage) -> float | None:
    sales = _coerce_float(sales_keur)
    margin = _coerce_float(margin_percentage)
    if sales is None or margin is None:
        return None
    return round((sales * margin) / 100, 2)


def _slugify_customer_name(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", _clean_text(value))
    ascii_text = text.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").upper()
    return slug or "UNKNOWN"


def _normalize_customer_name(value: str | None) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).casefold()


def upgrade() -> None:
    op.create_table(
        "potential",
        sa.Column(
            "rfq_id",
            sa.String(),
            sa.ForeignKey("rfq.rfq_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("potential_systematic_id", sa.String(), nullable=True, unique=True),
        sa.Column("customer", sa.Text(), nullable=True),
        sa.Column("customer_location", sa.Text(), nullable=True),
        sa.Column("application", sa.Text(), nullable=True),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("contact_phone", sa.String(), nullable=True),
        sa.Column("contact_function", sa.Text(), nullable=True),
        sa.Column("industry_served", sa.Text(), nullable=True),
        sa.Column("planned_product_type", sa.Text(), nullable=True),
        sa.Column("engagement_reasons", sa.Text(), nullable=True),
        sa.Column("idea_source", sa.Text(), nullable=True),
        sa.Column("current_supplier", sa.Text(), nullable=True),
        sa.Column("main_win_reason", sa.Text(), nullable=True),
        sa.Column("win_rationale_details", sa.Text(), nullable=True),
        sa.Column("technical_capabilities", sa.Text(), nullable=True),
        sa.Column("strategic_fit", sa.Text(), nullable=True),
        sa.Column("strategic_fit_details", sa.Text(), nullable=True),
        sa.Column("sales_keur", sa.Float(), nullable=True),
        sa.Column("margin_percentage", sa.Float(), nullable=True),
        sa.Column("margin_keur", sa.Float(), nullable=True),
        sa.Column("start_of_production", sa.Text(), nullable=True),
        sa.Column("development_effort", sa.Text(), nullable=True),
        sa.Column("side_effects", sa.Text(), nullable=True),
        sa.Column("risks_to_do", sa.Text(), nullable=True),
        sa.Column("risks_not_to_do", sa.Text(), nullable=True),
        sa.Column("chat_history", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    connection = op.get_bind()
    metadata = sa.MetaData()
    rfq_table = sa.Table(
        "rfq",
        metadata,
        sa.Column("rfq_id", sa.String()),
        sa.Column("sub_status", sa.String()),
        sa.Column("rfq_data", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime()),
    )
    potential_table = sa.Table(
        "potential",
        metadata,
        sa.Column("rfq_id", sa.String()),
        sa.Column("potential_systematic_id", sa.String()),
        sa.Column("customer", sa.Text()),
        sa.Column("customer_location", sa.Text()),
        sa.Column("application", sa.Text()),
        sa.Column("contact_name", sa.Text()),
        sa.Column("contact_email", sa.String()),
        sa.Column("contact_phone", sa.String()),
        sa.Column("contact_function", sa.Text()),
        sa.Column("industry_served", sa.Text()),
        sa.Column("planned_product_type", sa.Text()),
        sa.Column("engagement_reasons", sa.Text()),
        sa.Column("idea_source", sa.Text()),
        sa.Column("current_supplier", sa.Text()),
        sa.Column("main_win_reason", sa.Text()),
        sa.Column("win_rationale_details", sa.Text()),
        sa.Column("technical_capabilities", sa.Text()),
        sa.Column("strategic_fit", sa.Text()),
        sa.Column("strategic_fit_details", sa.Text()),
        sa.Column("sales_keur", sa.Float()),
        sa.Column("margin_percentage", sa.Float()),
        sa.Column("margin_keur", sa.Float()),
        sa.Column("start_of_production", sa.Text()),
        sa.Column("development_effort", sa.Text()),
        sa.Column("side_effects", sa.Text()),
        sa.Column("risks_to_do", sa.Text()),
        sa.Column("risks_not_to_do", sa.Text()),
        sa.Column("chat_history", postgresql.JSONB(astext_type=sa.Text())),
    )

    rows = connection.execute(
        sa.select(
            rfq_table.c.rfq_id,
            rfq_table.c.sub_status,
            rfq_table.c.rfq_data,
            rfq_table.c.created_at,
        ).order_by(rfq_table.c.created_at.asc(), rfq_table.c.rfq_id.asc())
    ).mappings()

    sequence_by_customer: dict[str, int] = {}
    potential_only_keys = {
        "potentialCustomerLocation",
        "potentialIndustry",
        "potentialProductType",
        "potentialEngagementReason",
        "potentialIdeaOwner",
        "potentialCurrentSupplier",
        "potentialWinReason",
        "potentialWinDetails",
        "potentialTechnicalCapability",
        "potentialStrategyFit",
        "potentialStrategyFitDetails",
        "potentialBusinessSalesKeur",
        "potentialBusinessMarginPercent",
        "potentialBusinessMarginKeur",
        "potentialStartOfProduction",
        "potentialDevelopmentEffort",
        "potentialSideEffects",
        "potentialRiskDoAssessment",
        "potentialRiskNotDoAssessment",
        "potential_risk_do_assessment",
        "potential_risk_do_assessments",
        "potential_risk_not_do_assessment",
        "potential_risk_not_do_assessments",
        "potential_chat_history",
    }
    shared_keys = {
        "customer_name",
        "application",
        "contact_name",
        "contact_email",
        "contact_phone",
        "contact_role",
    }

    for row in rows:
        rfq_data = dict(row["rfq_data"] or {})
        has_legacy_potential_data = bool(
            row["sub_status"] == "POTENTIAL"
            or "potential_chat_history" in rfq_data
            or any(key in rfq_data for key in potential_only_keys)
        )
        if not has_legacy_potential_data:
            continue

        customer = _pick_first(rfq_data, "customer_name", "customer", "client")
        customer_location = _pick_first(
            rfq_data,
            "customer_location",
            "customerLocation",
            "potential_customer_location",
            "potentialCustomerLocation",
        )
        application = _pick_first(rfq_data, "application")
        contact_name = _pick_first(
            rfq_data,
            "contact_name",
            "contactName",
            "contact_first_name",
        )
        contact_email = _pick_first(rfq_data, "contact_email", "contactEmail")
        contact_phone = _pick_first(rfq_data, "contact_phone", "contactPhone")
        contact_function = _pick_first(rfq_data, "contact_role", "contactFunction")
        sales_keur = _pick_first(
            rfq_data,
            "sales_keur",
            "potentialBusinessSalesKeur",
        )
        margin_percentage = _pick_first(
            rfq_data,
            "margin_percentage",
            "potentialBusinessMarginPercent",
        )
        margin_keur = _pick_first(
            rfq_data,
            "margin_keur",
            "potentialBusinessMarginKeur",
        )
        margin_keur = (
            _coerce_float(margin_keur)
            if margin_keur is not None
            else _calculate_margin_keur(sales_keur, margin_percentage)
        )

        normalized_customer = _normalize_customer_name(customer)
        potential_systematic_id = None
        if normalized_customer:
            next_count = sequence_by_customer.get(normalized_customer, 0) + 1
            sequence_by_customer[normalized_customer] = next_count
            potential_systematic_id = (
                f"POT-{next_count}-{_slugify_customer_name(customer)}"
            )

        connection.execute(
            potential_table.insert().values(
                rfq_id=row["rfq_id"],
                potential_systematic_id=potential_systematic_id,
                customer=_clean_text(customer) or None,
                customer_location=_clean_text(customer_location) or None,
                application=_clean_text(application) or None,
                contact_name=_clean_text(contact_name) or None,
                contact_email=_clean_text(contact_email) or None,
                contact_phone=_clean_text(contact_phone) or None,
                contact_function=_clean_text(contact_function) or None,
                industry_served=_clean_text(
                    _pick_first(rfq_data, "industry_served", "potentialIndustry")
                )
                or None,
                planned_product_type=_clean_text(
                    _pick_first(rfq_data, "planned_product_type", "potentialProductType")
                )
                or None,
                engagement_reasons=_clean_text(
                    _pick_first(rfq_data, "engagement_reasons", "potentialEngagementReason")
                )
                or None,
                idea_source=_clean_text(
                    _pick_first(rfq_data, "idea_source", "potentialIdeaOwner")
                )
                or None,
                current_supplier=_clean_text(
                    _pick_first(rfq_data, "current_supplier", "potentialCurrentSupplier")
                )
                or None,
                main_win_reason=_clean_text(
                    _pick_first(rfq_data, "main_win_reason", "potentialWinReason")
                )
                or None,
                win_rationale_details=_clean_text(
                    _pick_first(rfq_data, "win_rationale_details", "potentialWinDetails")
                )
                or None,
                technical_capabilities=_clean_text(
                    _pick_first(
                        rfq_data,
                        "technical_capabilities",
                        "potentialTechnicalCapability",
                    )
                )
                or None,
                strategic_fit=_clean_text(
                    _pick_first(rfq_data, "strategic_fit", "potentialStrategyFit")
                )
                or None,
                strategic_fit_details=_clean_text(
                    _pick_first(
                        rfq_data,
                        "strategic_fit_details",
                        "potentialStrategyFitDetails",
                    )
                )
                or None,
                sales_keur=_coerce_float(sales_keur),
                margin_percentage=_coerce_float(margin_percentage),
                margin_keur=margin_keur,
                start_of_production=_clean_text(
                    _pick_first(rfq_data, "start_of_production", "potentialStartOfProduction")
                )
                or None,
                development_effort=_clean_text(
                    _pick_first(rfq_data, "development_effort", "potentialDevelopmentEffort")
                )
                or None,
                side_effects=_clean_text(
                    _pick_first(rfq_data, "side_effects", "potentialSideEffects")
                )
                or None,
                risks_to_do=_clean_text(
                    _pick_first(
                        rfq_data,
                        "risks_to_do",
                        "potentialRiskDoAssessment",
                        "potential_risk_do_assessment",
                    )
                )
                or None,
                risks_not_to_do=_clean_text(
                    _pick_first(
                        rfq_data,
                        "risks_not_to_do",
                        "potentialRiskNotDoAssessment",
                        "potential_risk_not_do_assessment",
                    )
                )
                or None,
                chat_history=rfq_data.get("potential_chat_history") or [],
            )
        )

        updated_rfq_data = dict(rfq_data)
        for key in potential_only_keys:
            updated_rfq_data.pop(key, None)

        if row["sub_status"] == "POTENTIAL":
            for key in shared_keys:
                updated_rfq_data.pop(key, None)

        connection.execute(
            rfq_table.update()
            .where(rfq_table.c.rfq_id == row["rfq_id"])
            .values(rfq_data=updated_rfq_data)
        )


def downgrade() -> None:
    op.drop_table("potential")
