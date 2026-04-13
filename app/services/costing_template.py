from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from app.models.rfq import Rfq

FIELD_GROUPS: list[tuple[str, list[tuple[str, tuple[str, ...]]]]] = [
    (
        "Customer and product",
        [
            ("Customer", ("customer_name", "customer", "client")),
            ("Application", ("application",)),
            ("Product name", ("product_name", "productName")),
            ("Product line", ("product_line_acronym", "productLine")),
            ("Costing data", ("costing_data", "costingData")),
            ("Customer PN", ("customer_pn", "customerPn")),
            ("Revision level", ("revision_level", "revisionLevel")),
        ],
    ),
    (
        "Logistics and planning",
        [
            ("Delivery zone", ("delivery_zone", "deliveryZone")),
            ("Plant", ("delivery_plant", "plant")),
            ("Country", ("country",)),
            ("PO date", ("po_date", "poDate")),
            ("PPAP date", ("ppap_date", "ppapDate")),
            ("SOP year", ("sop_year", "sop")),
            ("Quantity per year", ("annual_volume", "qty_per_year", "qtyPerYear")),
            ("RFQ reception date", ("rfq_reception_date", "rfqReceptionDate")),
            ("Expected quotation date", ("quotation_expected_date", "expectedQuotationDate")),
        ],
    ),
    (
        "Contact details",
        [
            ("Contact name", ("contact_name", "contact_first_name", "contactName")),
            ("Contact function", ("contact_role", "contactFunction")),
            ("Contact phone", ("contact_phone", "contactPhone")),
            ("Contact email", ("contact_email", "contactEmail")),
        ],
    ),
    (
        "Commercial expectations",
        [
            ("Target price", ("target_price_eur", "targetPrice")),
            (
                "Expected delivery conditions",
                ("expected_delivery_conditions", "expectedDeliveryConditions"),
            ),
            ("Expected payment terms", ("expected_payment_terms", "expectedPaymentTerms")),
            ("Business trigger", ("business_trigger", "businessTrigger")),
            (
                "Customer tooling conditions",
                ("customer_tooling_conditions", "customerToolingConditions"),
            ),
            ("Entry barriers", ("entry_barriers", "entryBarriers")),
        ],
    ),
    (
        "Feasibility inputs",
        [
            (
                "Design responsible",
                ("responsibility_design", "design_responsible", "designResponsible"),
            ),
            (
                "Validation responsible",
                (
                    "responsibility_validation",
                    "validation_responsible",
                    "validationResponsible",
                ),
            ),
            ("Design owner", ("product_ownership", "design_owner", "designOwner")),
            (
                "Development costs",
                ("pays_for_development", "development_costs", "developmentCosts"),
            ),
            (
                "Technical capacity",
                ("capacity_available", "technical_capacity", "technicalCapacity"),
            ),
            ("Scope", ("scope",)),
            ("Customer status", ("customer_status", "customerStatus")),
            ("Strategic note", ("strategic_note", "strategicNote")),
            (
                "Final recommendation",
                ("is_feasible", "final_recommendation", "finalRecommendation"),
            ),
        ],
    ),
    (
        "Validation routing",
        [
            ("TO total", ("to_total", "toTotal")),
            (
                "Validator email",
                ("zone_manager_email", "validator_email", "validatorEmail"),
            ),
        ],
    ),
]

# (accent_color, header_bg, num_bg, num_color)
SECTION_ACCENTS: tuple[tuple[str, str, str, str], ...] = (
    ("#046eaf", "#eaf4fd", "#d3eaf9", "#046eaf"),
    ("#ef7807", "#fff2e6", "#ffe0c0", "#c05e00"),
    ("#0e4e78", "#e8f0f6", "#c6dcee", "#0e4e78"),
    ("#585858", "#f4f5f7", "#e2e4e8", "#444444"),
)

_BROWSER_CANDIDATE_PATHS = (
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_LOGO_PATH = PROJECT_ROOT / "Sales_App_Frontend" / "src" / "assets" / "logo.png"


def build_costing_template_filename(rfq: Rfq) -> str:
    data = dict(rfq.rfq_data or {})
    base_name = _stringify_value(data.get("systematic_rfq_id") or rfq.rfq_id)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._-") or "rfq"
    return f"{safe_name}_costing_feasibility_template.pdf"


def render_costing_template_pdf(rfq: Rfq) -> bytes:
    try:
        return _render_reportlab_pdf(rfq)
    except ImportError:
        document_html = render_costing_template_html(rfq)
        return _render_html_to_pdf(document_html)


def render_costing_template_html(rfq: Rfq) -> str:
    data = dict(rfq.rfq_data or {})
    if rfq.zone_manager_email and not _has_meaningful_value(data.get("zone_manager_email")):
        data["zone_manager_email"] = rfq.zone_manager_email
    if rfq.product_line_acronym and not _has_meaningful_value(data.get("product_line_acronym")):
        data["product_line_acronym"] = rfq.product_line_acronym

    systematic_rfq_id = _stringify_value(data.get("systematic_rfq_id") or "Pending assignment")
    approved_by = _stringify_value(data.get("zone_manager_email")) or None
    approval_date = _stringify_value(rfq.approved_at) or None
    generated_at = _stringify_value(datetime.now(timezone.utc))
    product_line = _pick_first_value(data, ("product_line_acronym", "productLine"))
    customer = _pick_first_value(data, ("customer_name", "customer", "client"))
    phase = _stringify_value(getattr(rfq.phase, "value", getattr(rfq, "phase", None))) or None
    sub_status = _stringify_value(getattr(rfq.sub_status, "value", getattr(rfq, "sub_status", None))) or None

    meta_cards = [
        ("RFQ ID", systematic_rfq_id),
        ("Created by", rfq.created_by_email),
        ("Approved by", approved_by),
        ("Approved at", approval_date),
        ("Customer", customer if customer != "-" else None),
        ("Product line", product_line if product_line != "-" else None),
    ]

    meta_cards_html = "".join(
        f"<tr>{''.join(f'<td>{_render_meta_card(label, value)}</td>' for label, value in meta_cards[i:i + 3])}</tr>"
        for i in range(0, len(meta_cards), 3)
    )

    badge_phase = f'<span class="pill pill-orange">Phase\u00a0: {escape(phase or "—")}</span>' if phase else ""
    badge_sub = f'<span class="pill">Sub-status\u00a0: {escape(sub_status or "—")}</span>' if sub_status else ""

    sections_html = "".join(
        _render_field_group(title, fields, data, index)
        for index, (title, fields) in enumerate(FIELD_GROUPS)
    )

    return f"""<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office"
      xmlns:w="urn:schemas-microsoft-com:office:word"
      xmlns="http://www.w3.org/TR/REC-html40">
<head>
  <meta charset="utf-8" />
  <title>RFQ Costing Feasibility Template</title>
  <style>
    @page {{
      size: A4;
      margin: 50pt 50pt 50pt 50pt;
    }}
    body {{
      font-family: Calibri, Arial, sans-serif;
      color: #1f2937;
      font-size: 11pt;
      line-height: 1.5;
      margin: 0;
      background: #f0f4f8;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    .sheet {{
      width: 100%;
    }}

    /* ── Hero ── */
    .hero {{
      background: linear-gradient(150deg, #0b3d6b 0%, #0e5a99 55%, #1271b5 100%);
      border-top: 5pt solid #ef7807;
      border-radius: 18pt;
      overflow: hidden;
      margin-bottom: 12pt;
    }}
    .hero-table {{
      width: 100%;
      border-collapse: collapse;
    }}
    .hero-table td {{
      border: none;
      vertical-align: top;
      padding: 0;
    }}
    .hero-brand {{
      width: 31%;
      padding: 24pt 20pt 20pt 20pt;
      background: rgba(0, 0, 0, 0.12);
    }}
    .hero-content {{
      width: 69%;
      padding: 24pt 28pt 24pt 28pt;
    }}
    .logo-wrap {{
      display: inline-block;
      background: rgba(255, 255, 255, 0.97);
      border-radius: 12pt;
      padding: 9pt 12pt;
      margin-bottom: 12pt;
    }}
    .logo-wrap img {{
      display: block;
      width: 132pt;
      height: auto;
    }}
    .eyebrow {{
      margin: 0 0 8pt 0;
      font-size: 7.5pt;
      font-weight: bold;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      color: rgba(255, 255, 255, 0.65);
    }}
    .hero-title {{
      margin: 0;
      font-size: 20pt;
      font-weight: bold;
      line-height: 1.1;
      color: #ffffff;
    }}
    .hero-copy {{
      margin: 10pt 0 0 0;
      font-size: 9.8pt;
      line-height: 1.72;
      color: rgba(255, 255, 255, 0.82);
    }}
    .kicker {{
      margin: 0 0 8pt 0;
      font-size: 7.5pt;
      font-weight: bold;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: rgba(255, 255, 255, 0.65);
    }}
    .content-title {{
      margin: 0 0 7pt 0;
      font-size: 16pt;
      font-weight: bold;
      line-height: 1.2;
      color: #ffffff;
    }}
    .content-copy {{
      margin: 0 0 12pt 0;
      font-size: 9.4pt;
      line-height: 1.72;
      color: rgba(255, 255, 255, 0.82);
    }}
    .pill-row {{
      margin-bottom: 12pt;
    }}
    .pill {{
      display: inline-block;
      margin: 0 5pt 5pt 0;
      padding: 5pt 10pt;
      border-radius: 999pt;
      background: rgba(255, 255, 255, 0.13);
      border: 1pt solid rgba(255, 255, 255, 0.22);
      color: #ffffff;
      font-size: 8pt;
      font-weight: bold;
    }}
    .pill-orange {{
      background: rgba(239, 120, 7, 0.22);
      border-color: rgba(239, 120, 7, 0.45);
    }}

    /* ── Meta cards ── */
    .meta-grid {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 8pt;
      margin: 0;
    }}
    .meta-grid td {{
      width: 33.33%;
      padding: 0;
      border: none;
      vertical-align: top;
    }}
    .meta-card {{
      min-height: 72pt;
      padding: 10pt 12pt;
      border-radius: 12pt;
      background: rgba(255, 255, 255, 0.97);
    }}
    .meta-label {{
      margin: 0 0 5pt 0;
      font-size: 7.5pt;
      font-weight: bold;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: #6b84a0;
    }}
    .meta-value {{
      margin: 0;
      font-size: 10.5pt;
      font-weight: bold;
      line-height: 1.4;
      color: #0e3a5c;
      word-break: break-word;
    }}
    .meta-value-empty {{
      margin: 0;
      font-size: 10pt;
      font-style: italic;
      color: #b0bec5;
    }}

    /* ── Document meta line ── */
    .doc-meta {{
      margin: 0 0 12pt 0;
      font-size: 9.4pt;
      color: #8a9ab0;
    }}
    .doc-meta-right {{
      float: right;
    }}

    /* ── Sections ── */
    .section {{
      margin-top: 16pt;
      border: 1pt solid #dce8f0;
      border-radius: 13pt;
      overflow: hidden;
      background: #ffffff;
    }}
    .section-top {{
      height: 3.5pt;
    }}
    .section-head {{
      padding: 10pt 14pt 8pt 14pt;
      border-bottom-width: 1pt;
      border-bottom-style: solid;
    }}
    .section-head-inner {{
      display: flex;
      align-items: center;
      gap: 12pt;
    }}
    .section-num {{
      display: inline-block;
      width: 22pt;
      height: 22pt;
      border-radius: 50%;
      text-align: center;
      line-height: 22pt;
      font-size: 8pt;
      font-weight: bold;
      flex-shrink: 0;
    }}
    h2 {{
      margin: 0;
      font-size: 12pt;
      font-weight: bold;
      color: #16344c;
      line-height: 1.2;
    }}
    .section-body {{
      padding: 4pt 14pt 12pt 14pt;
    }}
    table.fields {{
      border-collapse: collapse;
      width: 100%;
    }}
    table.fields td {{
      border: none;
      border-top: 0.8pt solid #eef3f7;
      padding: 6.5pt 0;
      vertical-align: top;
    }}
    table.fields tr:first-child td {{
      border-top: none;
    }}
    td.label {{
      width: 35%;
      padding-right: 16pt;
      padding-top: 7pt;
      font-weight: bold;
      font-size: 8pt;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: #7a94a8;
    }}
    td.value {{
      width: 65%;
      font-size: 10pt;
      line-height: 1.72;
      color: #223040;
      word-break: break-word;
    }}
    td.value-empty {{
      width: 65%;
      font-size: 10pt;
      line-height: 1.72;
      color: #b8c9d6;
      font-style: italic;
      word-break: break-word;
    }}

    /* ── Note ── */
    .note {{
      margin-top: 14pt;
      padding: 12pt 16pt;
      background: #fffbf5;
      border: 1pt solid #f5d9a8;
      border-left: 3.5pt solid #ef7807;
      border-radius: 12pt;
      color: #7a5e38;
      font-size: 9.4pt;
      line-height: 1.72;
    }}

    /* ── Footer ── */
    .footer {{
      margin-top: 18pt;
      padding-top: 10pt;
      border-top: 1pt solid #d8e6f0;
      font-size: 8pt;
      color: #a0b4c5;
    }}
    .footer-right {{
      float: right;
    }}
  </style>
</head>
<body>
  <div class="sheet">

    <div class="hero">
      <table class="hero-table">
        <tr>
          <td class="hero-brand">
            {_render_logo_html()}
            <p class="eyebrow">Costing handoff</p>
            <p class="hero-title">Costing<br />Feasibility<br />Template</p>
            <p class="hero-copy">Structured RFQ snapshot prepared for the costing review phase.</p>
          </td>
          <td class="hero-content">
            <p class="kicker">Review snapshot</p>
            <p class="content-title">Commercial-to-costing handoff</p>
            <p class="content-copy">
              The RFQ information below is organized for feasibility assessment, costing preparation, and internal alignment.
            </p>
            <div class="pill-row">
              {badge_phase}{badge_sub}
            </div>
            <table class="meta-grid">
              {meta_cards_html}
            </table>
          </td>
        </tr>
      </table>
    </div>

    <p class="doc-meta">
      <span class="doc-meta-right">Internal document - Restricted use</span>
      Generated on {escape(generated_at)}
    </p>

    {sections_html}

    <div class="note">
      <strong>Note:</strong> Empty fields are displayed as &mdash;.
      This document is generated automatically from the RFQ system and does not constitute a contractual commitment.
    </div>

  </div>
</body>
</html>
"""


def _render_reportlab_pdf(rfq: Rfq) -> bytes:
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def _paragraph_html(value: Any) -> str:
        text = _stringify_value(value)
        if not text or text == "-":
            return "&mdash;"
        return escape(text).replace("\n", "<br />")

    def _meta_card(label: str, value: Any) -> Table:
        card = Table(
            [[[
                Paragraph(escape(label.upper()), meta_label_style),
                Spacer(1, 3),
                Paragraph(_paragraph_html(value), meta_value_style),
            ]]],
            colWidths=[57 * mm],
        )
        card.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#d7e5ef")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return card

    def _pill(label: str, value: str, background: Any) -> Table:
        pill = Table(
            [[Paragraph(f"<b>{escape(label)}:</b> {escape(value)}", pill_style)]],
            colWidths=[56 * mm],
        )
        pill.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), background),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.white),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return pill

    data = dict(rfq.rfq_data or {})
    if rfq.zone_manager_email and not _has_meaningful_value(data.get("zone_manager_email")):
        data["zone_manager_email"] = rfq.zone_manager_email
    if rfq.product_line_acronym and not _has_meaningful_value(data.get("product_line_acronym")):
        data["product_line_acronym"] = rfq.product_line_acronym

    systematic_rfq_id = _stringify_value(data.get("systematic_rfq_id") or "Pending assignment")
    approved_by = _stringify_value(data.get("zone_manager_email")) or "-"
    approval_date = _stringify_value(rfq.approved_at) or "-"
    generated_at = _stringify_value(datetime.now(timezone.utc))
    product_line = _pick_first_value(data, ("product_line_acronym", "productLine"))
    customer = _pick_first_value(data, ("customer_name", "customer", "client"))
    phase = _stringify_value(getattr(rfq.phase, "value", getattr(rfq, "phase", None))) or "-"
    sub_status = _stringify_value(getattr(rfq.sub_status, "value", getattr(rfq, "sub_status", None))) or "-"

    tide = colors.HexColor("#046eaf")
    sun = colors.HexColor("#ef7807")
    mint = colors.HexColor("#0e4e78")
    ink = colors.HexColor("#16344c")
    text_muted = colors.HexColor("#6b84a0")
    text_soft = colors.HexColor("#d8e7f3")
    border_color = colors.HexColor("#d8e6f0")
    page_bg = colors.HexColor("#f0f4f8")

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="RFQ Costing Feasibility Template",
    )

    styles = getSampleStyleSheet()
    eyebrow_style = ParagraphStyle(
        "EyebrowStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=text_soft,
        spaceAfter=6,
    )
    hero_title_style = ParagraphStyle(
        "HeroTitleStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=22,
        textColor=colors.white,
        spaceAfter=8,
    )
    hero_body_style = ParagraphStyle(
        "HeroBodyStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.4,
        leading=14,
        textColor=text_soft,
    )
    content_kicker_style = ParagraphStyle(
        "ContentKickerStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=text_soft,
        spaceAfter=4,
    )
    content_title_style = ParagraphStyle(
        "ContentTitleStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.white,
        spaceAfter=6,
    )
    content_body_style = ParagraphStyle(
        "ContentBodyStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.2,
        leading=13.5,
        textColor=text_soft,
        spaceAfter=8,
    )
    pill_style = ParagraphStyle(
        "PillStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.8,
        leading=9,
        textColor=colors.white,
    )
    meta_label_style = ParagraphStyle(
        "MetaLabelStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.2,
        leading=8.4,
        textColor=text_muted,
    )
    meta_value_style = ParagraphStyle(
        "MetaValueStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=ink,
    )
    meta_line_style = ParagraphStyle(
        "MetaLineStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=text_muted,
    )
    section_number_style = ParagraphStyle(
        "SectionNumberStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        alignment=1,
        textColor=colors.white,
    )
    section_title_style = ParagraphStyle(
        "SectionTitleStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=14,
        textColor=ink,
    )
    field_label_style = ParagraphStyle(
        "FieldLabelStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.3,
        leading=10,
        textColor=text_muted,
    )
    field_value_style = ParagraphStyle(
        "FieldValueStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.4,
        leading=13,
        textColor=ink,
    )
    field_empty_style = ParagraphStyle(
        "FieldEmptyStyle",
        parent=field_value_style,
        textColor=colors.HexColor("#b8c9d6"),
        fontName="Helvetica-Oblique",
    )
    note_style = ParagraphStyle(
        "NoteStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13.5,
        textColor=colors.HexColor("#7a5e38"),
    )
    footer_style = ParagraphStyle(
        "FooterStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.8,
        leading=10,
        textColor=text_muted,
    )

    story: list[Any] = []

    brand_flowables: list[Any] = []
    if FRONTEND_LOGO_PATH.exists():
        logo = Image(str(FRONTEND_LOGO_PATH))
        logo.drawWidth = 38 * mm
        if getattr(logo, "imageWidth", 0):
            logo.drawHeight = logo.imageHeight * logo.drawWidth / logo.imageWidth
        brand_flowables.extend([logo, Spacer(1, 10)])
    brand_flowables.extend(
        [
            Paragraph("COSTING HANDOFF", eyebrow_style),
            Paragraph("Costing<br/>Feasibility<br/>Template", hero_title_style),
            Paragraph(
                "Structured RFQ snapshot prepared for the costing review phase.",
                hero_body_style,
            ),
        ]
    )

    pills_row = Table(
        [[_pill("Phase", phase, colors.HexColor("#2a86c2")), _pill("Sub-status", sub_status, sun)]],
        colWidths=[58 * mm, 58 * mm],
    )
    pills_row.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    meta_cards = [
        ("RFQ ID", systematic_rfq_id),
        ("Created by", rfq.created_by_email),
        ("Approved by", approved_by),
        ("Approved at", approval_date),
        ("Customer", customer if customer != "-" else None),
        ("Product line", product_line if product_line != "-" else None),
    ]
    meta_rows = []
    for index in range(0, len(meta_cards), 2):
        row = []
        for label, value in meta_cards[index:index + 2]:
            row.append(_meta_card(label, value))
        while len(row) < 2:
            row.append("")
        meta_rows.append(row)

    meta_grid = Table(meta_rows, colWidths=[58 * mm, 58 * mm])
    meta_grid.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    content_flowables = [
        Paragraph("REVIEW SNAPSHOT", content_kicker_style),
        Paragraph("Commercial-to-costing handoff", content_title_style),
        Paragraph(
            "The RFQ information below is organized for feasibility assessment, costing preparation, and internal alignment.",
            content_body_style,
        ),
        pills_row,
        Spacer(1, 10),
        meta_grid,
    ]

    hero = Table([[brand_flowables, content_flowables]], colWidths=[56 * mm, 124 * mm])
    hero.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), tide),
                ("BACKGROUND", (0, 0), (0, 0), mint),
                ("LINEABOVE", (0, 0), (-1, 0), 4, sun),
                ("BOX", (0, 0), (-1, -1), 0.8, tide),
                ("LEFTPADDING", (0, 0), (0, 0), 16),
                ("RIGHTPADDING", (0, 0), (0, 0), 16),
                ("TOPPADDING", (0, 0), (0, 0), 18),
                ("BOTTOMPADDING", (0, 0), (0, 0), 18),
                ("LEFTPADDING", (1, 0), (1, 0), 18),
                ("RIGHTPADDING", (1, 0), (1, 0), 18),
                ("TOPPADDING", (1, 0), (1, 0), 18),
                ("BOTTOMPADDING", (1, 0), (1, 0), 18),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.extend([hero, Spacer(1, 10)])

    story.append(
        Paragraph(
            f"Generated on {escape(generated_at)}<br/>Internal document - Restricted use",
            meta_line_style,
        )
    )
    story.append(Spacer(1, 10))

    for index, (title, fields) in enumerate(FIELD_GROUPS):
        accent, head_bg, _, _ = SECTION_ACCENTS[index % len(SECTION_ACCENTS)]
        accent_color = colors.HexColor(accent)
        heading_bg = colors.HexColor(head_bg)

        header = Table(
            [[
                Paragraph(f"{index + 1:02d}", section_number_style),
                Paragraph(escape(title), section_title_style),
            ]],
            colWidths=[14 * mm, 166 * mm],
        )
        header.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), accent_color),
                    ("BACKGROUND", (1, 0), (1, 0), heading_bg),
                    ("BOX", (0, 0), (-1, -1), 0.8, border_color),
                    ("LEFTPADDING", (0, 0), (0, 0), 0),
                    ("RIGHTPADDING", (0, 0), (0, 0), 0),
                    ("TOPPADDING", (0, 0), (0, 0), 7),
                    ("BOTTOMPADDING", (0, 0), (0, 0), 7),
                    ("LEFTPADDING", (1, 0), (1, 0), 12),
                    ("RIGHTPADDING", (1, 0), (1, 0), 12),
                    ("TOPPADDING", (1, 0), (1, 0), 7),
                    ("BOTTOMPADDING", (1, 0), (1, 0), 7),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )

        field_rows = []
        for label, keys in fields:
            raw = _pick_first_value(data, keys)
            value_style = field_empty_style if raw == "-" else field_value_style
            field_rows.append(
                [
                    Paragraph(escape(label.upper()), field_label_style),
                    Paragraph(_paragraph_html(raw), value_style),
                ]
            )

        body = Table(field_rows, colWidths=[56 * mm, 124 * mm])
        body.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.8, border_color),
                    ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#eef3f7")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )

        story.extend([header, body, Spacer(1, 10)])

    note = Table(
        [[Paragraph(
            "<b>Note:</b> Empty fields are displayed as &mdash;. This document is generated automatically from the RFQ system and does not constitute a contractual commitment.",
            note_style,
        )]],
        colWidths=[180 * mm],
    )
    note.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbf5")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#f5d9a8")),
                ("LINEBEFORE", (0, 0), (0, 0), 3, sun),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.extend([note, Spacer(1, 10)])

    footer = Table(
        [[Paragraph("AVO Carbon Group - Costing feasibility handoff", footer_style)]],
        colWidths=[180 * mm],
    )
    footer.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 0.7, page_bg),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(footer)

    document.build(story)
    return buffer.getvalue()

def _render_html_to_pdf(document_html: str) -> bytes:
    browser_path = _find_browser_executable()
    if browser_path is None:
        raise RuntimeError(
            "No compatible browser was found for PDF generation. "
            "Install Microsoft Edge or Google Chrome."
        )

    with tempfile.TemporaryDirectory(prefix="costing-template-") as temp_dir:
        temp_path = Path(temp_dir)
        html_path = temp_path / "costing-template.html"
        pdf_path = temp_path / "costing-template.pdf"
        html_path.write_text(document_html, encoding="utf-8")

        command = [
            str(browser_path),
            "--headless",
            "--disable-gpu",
            f"--print-to-pdf={pdf_path}",
            "--print-to-pdf-no-header",
            "--no-pdf-header-footer",
            html_path.resolve().as_uri(),
        ]

        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(
                f"PDF generation failed: {stderr or 'browser render error'}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("PDF generation timed out.") from exc

        if not pdf_path.exists():
            raise RuntimeError("PDF generation did not produce an output file.")

        return pdf_path.read_bytes()


@lru_cache(maxsize=1)
def _find_browser_executable() -> Path | None:
    for candidate in _BROWSER_CANDIDATE_PATHS:
        if candidate.exists():
            return candidate
    for executable in ("msedge", "msedge.exe", "chrome", "chrome.exe"):
        resolved = shutil.which(executable)
        if resolved:
            return Path(resolved)
    return None


def _render_field_group(
    title: str,
    fields: list[tuple[str, tuple[str, ...]]],
    data: dict[str, Any],
    index: int,
) -> str:
    accent, head_bg, num_bg, num_color = SECTION_ACCENTS[index % len(SECTION_ACCENTS)]
    section_number = index + 1
    rows: list[str] = []

    for label, keys in fields:
        raw = _pick_first_value(data, keys)
        is_empty = raw == "-"
        cell_class = "value-empty" if is_empty else "value"
        cell_content = "&mdash;" if is_empty else _format_html_value(raw)
        rows.append(
            f'<tr>'
            f'<td class="label">{escape(label)}</td>'
            f'<td class="{cell_class}">{cell_content}</td>'
            f'</tr>'
        )

    border_color = accent + "33"

    return f"""
    <div class="section">
      <div class="section-top" style="background:{accent};"></div>
      <div class="section-head" style="background:{head_bg}; border-bottom-color:{border_color};">
        <div class="section-head-inner">
          <span class="section-num" style="background:{num_bg}; color:{num_color};">{section_number}</span>
          <h2>{escape(title)}</h2>
        </div>
      </div>
      <div class="section-body">
        <table class="fields">{''.join(rows)}</table>
      </div>
    </div>
    """


def _render_meta_card(label: str, value: Any) -> str:
    text = _stringify_value(value) if value is not None else ""
    if not text or text == "-":
        value_html = '<p class="meta-value-empty">&mdash;</p>'
    else:
        value_html = f'<p class="meta-value">{escape(text)}</p>'
    return (
        '<div class="meta-card">'
        f'<p class="meta-label">{escape(label)}</p>'
        f'{value_html}'
        '</div>'
    )


def _render_logo_html() -> str:
    data_uri = _load_logo_data_uri()
    if not data_uri:
        return ""
    return f'<div class="logo-wrap"><img src="{data_uri}" alt="Avo Carbon Group" /></div>'


@lru_cache(maxsize=1)
def _load_logo_data_uri() -> str:
    try:
        content = FRONTEND_LOGO_PATH.read_bytes()
    except OSError:
        return ""
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _pick_first_value(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if _has_meaningful_value(value):
            return _stringify_value(value)
    return "-"


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        dt_value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt_value.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        parsed = _parse_datetime_string(text)
        if parsed:
            return parsed
        return text
    if isinstance(value, dict):
        parts = [
            f"{key}: {_stringify_value(item)}"
            for key, item in value.items()
            if _has_meaningful_value(item)
        ]
        return "\n".join(parts)
    if isinstance(value, (list, tuple, set)):
        parts = [_stringify_value(item) for item in value if _has_meaningful_value(item)]
        return "\n".join(part for part in parts if part)
    return str(value)


def _parse_datetime_string(value: str) -> str:
    normalized = value.replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(normalized)
        except ValueError:
            continue
        if "T" in value or " " in value:
            dt_value = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return dt_value.strftime("%Y-%m-%d %H:%M UTC")
        return parsed.strftime("%Y-%m-%d")
    return ""


def _format_html_value(value: Any) -> str:
    text = _stringify_value(value)
    if not text or text == "-":
        return "&mdash;"
    return escape(text).replace("\n", "<br />")
