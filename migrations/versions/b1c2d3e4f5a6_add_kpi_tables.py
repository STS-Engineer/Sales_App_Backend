"""add kpi tables

Revision ID: b1c2d3e4f5a6
Revises: 4e6a1c9b2d3f
Create Date: 2026-06-17 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "4e6a1c9b2d3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kpi_annual_target",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("year", sa.Integer(), unique=True, nullable=False),
        sa.Column("total_ca_meur", sa.Float(), nullable=True),
        sa.Column("renewal_pct", sa.Float(), nullable=False, server_default="25.0"),
        sa.Column("rfq_automotive_monthly_target", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("rfq_non_auto_monthly_target", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("new_business_monthly_keur", sa.Float(), nullable=False, server_default="2000.0"),
        sa.Column("excluded_zones", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sites", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("salesperson_targets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "kpi_opportunity",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("customer", sa.String(), nullable=False),
        sa.Column("product_line", sa.String(), nullable=True),
        sa.Column("site", sa.String(), nullable=True),
        sa.Column("zone", sa.String(), nullable=True),
        sa.Column("salesperson_email", sa.String(), nullable=True),
        sa.Column("annual_keur", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("probability", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("sector", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_kpi_opportunity_year", "kpi_opportunity", ["year"])
    op.create_index("ix_kpi_opportunity_salesperson", "kpi_opportunity", ["salesperson_email"])

    op.create_table(
        "kpi_new_business",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("customer", sa.String(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=True),
        sa.Column("product_category", sa.String(), nullable=True),
        sa.Column("product_line", sa.String(), nullable=True),
        sa.Column("zone", sa.String(), nullable=True),
        sa.Column("site", sa.String(), nullable=True),
        sa.Column("salesperson_email", sa.String(), nullable=True),
        sa.Column("annual_keur", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("sop", sa.String(), nullable=True),
        sa.Column("sector", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_kpi_new_business_year", "kpi_new_business", ["year"])
    op.create_index("ix_kpi_new_business_salesperson", "kpi_new_business", ["salesperson_email"])


def downgrade() -> None:
    op.drop_index("ix_kpi_new_business_salesperson", "kpi_new_business")
    op.drop_index("ix_kpi_new_business_year", "kpi_new_business")
    op.drop_table("kpi_new_business")
    op.drop_index("ix_kpi_opportunity_salesperson", "kpi_opportunity")
    op.drop_index("ix_kpi_opportunity_year", "kpi_opportunity")
    op.drop_table("kpi_opportunity")
    op.drop_table("kpi_annual_target")
