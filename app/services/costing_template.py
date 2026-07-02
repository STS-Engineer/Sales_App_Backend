from __future__ import annotations

import base64
import os
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
from app.schemas.rfq import normalize_rfq_data_products

# Section marker sentinel used for table-only sections (Products / Volumes).
# The rendering layer checks the section title and calls the appropriate table builder.
_TABLE_SECTION: list[tuple[str, tuple[str, ...]]] = []

FIELD_GROUPS: list[tuple[str, list[tuple[str, tuple[str, ...]]]]] = [
    (
        "Customer details",
        [
            ("Automotive type", ("automotive_type", "automotiveType")),
            ("Customer", ("customer_name", "customer", "client")),
            ("Project name", ("project_name", "projectName")),
        ],
    ),
    ("Products", _TABLE_SECTION),    # rendered as a Products info table
    ("Volumes", _TABLE_SECTION),     # rendered as a Volumes table
    (
        "Logistics details",
        [
            ("Expected PO date", ("po_date", "poDate")),
            ("Expected PPAP date", ("ppap_date", "ppapDate")),
            ("RFQ reception date", ("rfq_reception_date", "rfqReceptionDate")),
            ("Expected quotation date", ("quotation_expected_date", "expectedQuotationDate")),
        ],
    ),
    (
        "Customer contact details",
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
            ("Delivery Incoterm", ("delivery_incoterm", "deliveryIncoterm", "expected_delivery_conditions", "expectedDeliveryConditions")),
            ("Incoterm Location", ("incoterm_location", "incotermLocation")),
            ("Expected payment terms", ("expected_payment_terms", "expectedPaymentTerms")),
            ("Type of packaging", ("type_of_packaging", "typeOfPackaging")),
            ("Business trigger", ("business_trigger", "businessTrigger")),
            ("Customer tooling conditions", ("customer_tooling_conditions", "customerToolingConditions")),
            ("Entry barriers", ("entry_barriers", "entryBarriers")),
        ],
    ),
    (
        "Commercial questions",
        [
            ("Design responsible", ("responsibility_design", "design_responsible", "designResponsible")),
            ("Validation responsible", ("responsibility_validation", "validation_responsible", "validationResponsible")),
            ("Design owner", ("product_ownership", "design_owner", "designOwner")),
            ("Development costs", ("pays_for_development", "development_costs", "developmentCosts")),
            ("Technical capacity", ("capacity_available", "technical_capacity", "technicalCapacity")),
            ("Scope", ("scope",)),
            ("Strategic note", ("strategic_note", "strategicNote")),
            ("Final recommendation", ("is_feasible", "final_recommendation", "finalRecommendation")),
        ],
    ),
    (
        "RFQ validation and submission",
        [
            ("Total Turnover", ("to_total", "toTotal")),
            ("Validator Email", ("zone_manager_email", "validator_email", "validatorEmail")),
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

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = BACKEND_ROOT.parent
LOGO_CANDIDATE_PATHS = (
    BACKEND_ROOT / "app" / "assets" / "logo.png",
    DEPLOY_ROOT / "Sales_App_Frontend" / "src" / "assets" / "logo.png",
)

_WKHTMLTOPDF_CANDIDATE_PATHS = (
    BACKEND_ROOT / "vendor" / "wkhtmltopdf" / "usr" / "local" / "bin" / "wkhtmltopdf",
    BACKEND_ROOT / "vendor" / "wkhtmltopdf" / "usr" / "bin" / "wkhtmltopdf",
    Path("/usr/bin/wkhtmltopdf"),
    Path("/usr/local/bin/wkhtmltopdf"),
    Path(r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"),
    Path(r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe"),
)

_BROWSER_CANDIDATE_PATHS = (
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)



def _build_costing_template_data(rfq: Rfq) -> dict[str, Any]:
    data = dict(normalize_rfq_data_products(rfq.rfq_data) or {})
    if rfq.zone_manager_email and not _has_meaningful_value(data.get("zone_manager_email")):
        data["zone_manager_email"] = rfq.zone_manager_email
    if rfq.product_line_acronym and not _has_meaningful_value(data.get("product_line_acronym")):
        data["product_line_acronym"] = rfq.product_line_acronym
    return data


def build_costing_template_filename(rfq: Rfq) -> str:
    data = dict(rfq.rfq_data or {})
    base_name = _stringify_value(data.get("systematic_rfq_id") or rfq.rfq_id)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._-") or "rfq"
    return f"{safe_name}_costing_feasibility_template.pdf"


def render_costing_template_pdf(rfq: Rfq) -> bytes:
    return _render_reportlab_pdf(rfq)

def render_costing_template_html(rfq: Rfq) -> str:
    data = _build_costing_template_data(rfq)
    systematic_rfq_id = _stringify_value(data.get("systematic_rfq_id") or "Pending assignment")
    approved_by = _stringify_value(data.get("zone_manager_email")) or None
    approval_date = _stringify_value(rfq.approved_at) or None
    generated_at = _stringify_value(datetime.now(timezone.utc))
    product_line = _pick_first_value(data, ("product_line_acronym", "productLine"))
    customer = _pick_first_value(data, ("customer_name", "customer", "client"))
    phase = _stringify_value(getattr(rfq.phase, "value", getattr(rfq, "phase", None))) or None
    sub_status = _stringify_value(getattr(rfq.sub_status, "value", getattr(rfq, "sub_status", None))) or None
    products_info_rows = _build_products_info_rows(data)
    volumes_rows_data = _build_volumes_rows(data)

    meta_cards = [
        ("RFQ ID", systematic_rfq_id),
        ("Created by", rfq.created_by_email),
        ("Approved by", approved_by),
    ]

    meta_cards_html = "".join(
        f"<tr>{''.join(f'<td>{_render_meta_card(label, value)}</td>' for label, value in meta_cards[i:i + 3])}</tr>"
        for i in range(0, len(meta_cards), 3)
    )

    badge_phase = f'<span class="pill pill-orange">Phase\u00a0: {escape(phase or "—")}</span>' if phase else ""
    badge_sub = f'<span class="pill">Sub-status\u00a0: {escape(sub_status or "—")}</span>' if sub_status else ""

    sections_html = "".join(
        _render_field_group(
            title, fields, data, index,
            products_info_rows=products_info_rows,
            volumes_rows_data=volumes_rows_data,
        )
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
      margin: 28pt 28pt 28pt 28pt;
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
      padding: 9pt 8pt;
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
    .section-copy {{
      margin: 2pt 0 10pt 0;
      font-size: 9pt;
      line-height: 1.6;
      color: #607588;
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
    table.reference-table {{
      width: 100%;
      border-collapse: collapse;
      border: 1pt solid #dce8f0;
      border-radius: 10pt;
      overflow: hidden;
    }}
    table.reference-table th {{
      padding: 7pt 8pt;
      border: none;
      background: #0e4e78;
      color: #ffffff;
      font-size: 7.6pt;
      font-weight: bold;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      text-align: left;
    }}
    table.reference-table td {{
      padding: 7pt 8pt;
      border: none;
      border-top: 0.8pt solid #eef3f7;
      font-size: 9.2pt;
      line-height: 1.55;
      color: #223040;
      vertical-align: top;
      word-break: break-word;
    }}
    table.reference-table tr:first-child td {{
      border-top: none;
    }}
    table.reference-table tbody tr:nth-child(even) td {{
      background: #f8fbfd;
    }}
    .reference-index {{
      width: 5%;
      color: #6b84a0;
      font-weight: bold;
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

    {sections_html}

    <div class="note">
      <strong>Note:</strong> Empty fields are displayed as &mdash;.
      This document is generated automatically from the RFQ system and does not constitute a contractual commitment.
    </div>

  </div>
</body>
</html>
"""


# ── PDF rendering ────────────────────────────────────────────────────────────


def _render_html_to_pdf(document_html: str) -> bytes:
    """
    Tries renderers in order of preference:
      1. wkhtmltopdf  (best fidelity, no Python deps beyond the binary)
      2. Chrome/Edge headless  (fallback for Windows local dev)
    Raises RuntimeError if none is available.
    """
    wkhtmltopdf_path = _find_wkhtmltopdf_executable()
    if wkhtmltopdf_path is not None:
        return _render_html_to_pdf_with_wkhtmltopdf(document_html, wkhtmltopdf_path)

    browser_path = _find_browser_executable()
    if browser_path is not None:
        return _render_html_to_pdf_with_browser(document_html, browser_path)

    raise RuntimeError(
        "No PDF renderer found. "
        "Install wkhtmltopdf (apt-get install wkhtmltopdf) "
        "or add Chrome / Edge to the system PATH."
    )


def _render_html_to_pdf_with_wkhtmltopdf(document_html: str, wkhtmltopdf_path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="costing-template-") as temp_dir:
        temp_path = Path(temp_dir)
        html_path = temp_path / "costing-template.html"
        pdf_path = temp_path / "costing-template.pdf"
        html_path.write_text(document_html, encoding="utf-8")

        command = [
            str(wkhtmltopdf_path),
            "--page-size", "A4",
            "--margin-top", "28pt",
            "--margin-right", "28pt",
            "--margin-bottom", "28pt",
            "--margin-left", "28pt",
            "--no-outline",
            "--print-media-type",
            "--enable-local-file-access",
            "--disable-smart-shrinking",
            "--no-header-line",
            "--no-footer-line",
            "--quiet",
            str(html_path),
            str(pdf_path),
        ]

        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=_build_wkhtmltopdf_env(wkhtmltopdf_path, temp_path),
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(
                f"wkhtmltopdf PDF generation failed: {stderr or 'unknown error'}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("wkhtmltopdf PDF generation timed out.") from exc

        if not pdf_path.exists():
            raise RuntimeError("wkhtmltopdf did not produce an output file.")

        return pdf_path.read_bytes()


def _render_html_to_pdf_with_browser(document_html: str, browser_path: Path) -> bytes:
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
                f"Browser PDF generation failed: {stderr or 'browser render error'}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Browser PDF generation timed out.") from exc

        if not pdf_path.exists():
            raise RuntimeError("Browser PDF generation did not produce an output file.")

        return pdf_path.read_bytes()


def _build_wkhtmltopdf_env(wkhtmltopdf_path: Path, temp_path: Path) -> dict[str, str]:
    env = os.environ.copy()

    candidate_lib_dirs: list[str] = []
    for parent in wkhtmltopdf_path.parents:
        for relative in (
            Path("../lib"),
            Path("../../lib"),
            Path("../lib/x86_64-linux-gnu"),
            Path("../../lib/x86_64-linux-gnu"),
            Path("../../usr/local/lib"),
            Path("../../usr/lib"),
        ):
            try:
                resolved = (parent / relative).resolve()
            except OSError:
                continue
            if resolved.exists() and resolved.is_dir():
                candidate_lib_dirs.append(str(resolved))

    seen: set[str] = set()
    ordered_lib_dirs: list[str] = []
    for item in candidate_lib_dirs:
        if item in seen:
            continue
        seen.add(item)
        ordered_lib_dirs.append(item)

    existing_ld = env.get("LD_LIBRARY_PATH", "")
    ld_parts = ordered_lib_dirs + ([existing_ld] if existing_ld else [])
    if ld_parts:
        env["LD_LIBRARY_PATH"] = ":".join(part for part in ld_parts if part)

    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("XDG_RUNTIME_DIR", str(temp_path))
    env.setdefault("HOME", str(temp_path))
    return env


@lru_cache(maxsize=1)
def _find_wkhtmltopdf_executable() -> Path | None:
    for candidate in _WKHTMLTOPDF_CANDIDATE_PATHS:
        if candidate.exists():
            return candidate
    resolved = shutil.which("wkhtmltopdf")
    if resolved:
        return Path(resolved)
    return None


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


# ── ReportLab fallback ───────────────────────────────────────────────────────


def _render_reportlab_pdf(rfq: Rfq) -> bytes:
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Flowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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

    data = _build_costing_template_data(rfq)
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
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="RFQ Costing Feasibility Template",
    )

    styles = getSampleStyleSheet()
    eyebrow_style = ParagraphStyle("EyebrowStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=text_soft, spaceAfter=6)
    hero_title_style = ParagraphStyle("HeroTitleStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=20, leading=22, textColor=colors.white, spaceAfter=8)
    hero_body_style = ParagraphStyle("HeroBodyStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9.4, leading=14, textColor=text_soft)
    content_kicker_style = ParagraphStyle("ContentKickerStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=text_soft, spaceAfter=4)
    content_title_style = ParagraphStyle(
        "ContentTitleStyle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=16,
        textColor=colors.white,
        spaceAfter=5,
    )

    content_body_style = ParagraphStyle(
        "ContentBodyStyle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.7,
        leading=12.4,
        textColor=text_soft,
        spaceAfter=6,
    )
    pill_style = ParagraphStyle("PillStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=7.8, leading=9, textColor=colors.white)
    meta_label_style = ParagraphStyle("MetaLabelStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.2, leading=8.4, textColor=text_muted)
    meta_value_style = ParagraphStyle("MetaValueStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=ink)
    header_card_label_style = ParagraphStyle("HeaderCardLabelStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7, leading=8.2, textColor=text_muted)
    header_card_value_style = ParagraphStyle("HeaderCardValueStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.8, leading=11.1, textColor=ink)
    meta_line_style = ParagraphStyle("MetaLineStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=11, textColor=text_muted)
    section_number_style = ParagraphStyle("SectionNumberStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9, leading=11, alignment=1, textColor=colors.white)
    section_title_style = ParagraphStyle("SectionTitleStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=12, leading=14, textColor=ink)
    section_copy_style = ParagraphStyle("SectionCopyStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=8.7, leading=12, textColor=text_muted)
    field_label_style = ParagraphStyle("FieldLabelStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.3, leading=10, textColor=text_muted)
    field_value_style = ParagraphStyle("FieldValueStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9.4, leading=13, textColor=ink)
    field_empty_style = ParagraphStyle("FieldEmptyStyle", parent=field_value_style, textColor=colors.HexColor("#b8c9d6"), fontName="Helvetica-Oblique")
    reference_header_style = ParagraphStyle("ReferenceHeaderStyle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.1, leading=8.5, textColor=colors.white)
    reference_value_style = ParagraphStyle("ReferenceValueStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=8.6, leading=11.2, textColor=ink)
    reference_empty_style = ParagraphStyle("ReferenceEmptyStyle", parent=reference_value_style, textColor=colors.HexColor("#b8c9d6"), fontName="Helvetica-Oblique")
    note_style = ParagraphStyle("NoteStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=13.5, textColor=colors.HexColor("#7a5e38"))
    footer_style = ParagraphStyle("FooterStyle", parent=styles["Normal"], fontName="Helvetica", fontSize=7.8, leading=10, textColor=text_muted)

    story: list[Any] = []

    meta_cards_rl = [
        ("RFQ ID", systematic_rfq_id),
        ("Created by", rfq.created_by_email),
        ("Approved by", approved_by),
    ]

    class ReportLabHeroHeader(Flowable):
        def __init__(self) -> None:
            super().__init__()
            self.card_rows = max(1, (len(meta_cards_rl) + 2) // 3)
            self.width = document.width
            self.hero_width = 180 * mm
            self.height = 76 * mm if self.card_rows == 1 else 116 * mm

        def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
            return self.width, self.height

        def draw(self) -> None:
            canv = self.canv
            width = self.hero_width
            height = self.height
            hero_x = 3 * mm
            left_width = 57 * mm
            right_x = left_width
            outer_radius = 5 * mm
            panel_radius = 4 * mm
            card_gap = 2.5 * mm
            card_height = 29 * mm
            card_width = (width - right_x - 11 * mm - (2 * card_gap)) / 3
            cards_x = right_x + 5.5 * mm
            row_gap = 4 * mm
            card_rows = self.card_rows

            def draw_paragraph(text_value: str, style: ParagraphStyle, x: float, top_y: float, max_width: float) -> float:
                paragraph = Paragraph(text_value, style)
                _, para_height = paragraph.wrap(max_width, 1000)
                paragraph.drawOn(canv, x, top_y - para_height)
                return para_height

            def draw_pill(text_value: str, x: float, y: float, width_value: float, fill_color: Any, stroke_color: Any) -> None:
                pill_height = 9.5 * mm
                canv.saveState()
                canv.setFillColor(fill_color)
                canv.setStrokeColor(stroke_color)
                canv.setLineWidth(1)
                canv.roundRect(x, y, width_value, pill_height, 4.25 * mm, fill=1, stroke=1)
                pill_paragraph = Paragraph(text_value, pill_style)
                _, pill_text_height = pill_paragraph.wrap(width_value - 8 * mm, pill_height - 1 * mm)
                pill_paragraph.drawOn(canv, x + 4 * mm, y + (pill_height - pill_text_height) / 2 - 0.3 * mm)
                canv.restoreState()

            def draw_card(label: str, value: Any, x: float, y: float) -> None:
                canv.saveState()
                canv.setFillColor(colors.white)
                canv.setStrokeColor(colors.white)
                canv.roundRect(x, y, card_width, card_height, 5 * mm, fill=1, stroke=0)
                label_paragraph = Paragraph(escape(label.upper()), header_card_label_style)
                _, label_height = label_paragraph.wrap(card_width - 10 * mm, 1000)
                label_paragraph.drawOn(canv, x + 5 * mm, y + card_height - 6 * mm - label_height)
                value_text = _paragraph_html(value)
                value_paragraph = Paragraph(value_text, header_card_value_style)
                _, value_height = value_paragraph.wrap(card_width - 10 * mm, card_height - 14 * mm)
                value_paragraph.drawOn(canv, x + 5 * mm, y + card_height - 10 * mm - label_height - value_height)
                canv.restoreState()

            canv.saveState()
            canv.translate(hero_x, 0)
            canv.setFillColor(tide)
            canv.roundRect(0, 0, width, height, outer_radius, fill=1, stroke=0)

            # Bloc gauche avec coins arrondis à gauche seulement
            path = canv.beginPath()
            r = outer_radius

            path.moveTo(0, r)
            path.arcTo(0, 0, 2 * r, 2 * r, startAng=180, extent=-90)              # bas gauche
            path.lineTo(left_width, 0)                                             # bas droit
            path.lineTo(left_width, height)                                        # haut droit
            path.lineTo(r, height)                                                 # vers haut gauche
            path.arcTo(0, height - 2 * r, 2 * r, height, startAng=90, extent=90)  # haut gauche
            path.close()

            canv.setFillColor(mint)
            canv.drawPath(path, fill=1, stroke=0)

            canv.setFillColor(sun)
            canv.roundRect(7 * mm, height - 1.7 * mm, width - 14 * mm, 1.2 * mm, 0.6 * mm, fill=1, stroke=0)

            logo_path = _resolve_logo_path()
            if logo_path:
                reader = ImageReader(str(logo_path))
                image_width, image_height = reader.getSize()

                logo_box_x = 7.0 * mm
                logo_box_y = height - 15.0 * mm
                logo_box_width = 44 * mm
                logo_box_height = 12.0 * mm

                canv.setFillColor(colors.white)
                canv.roundRect(
                    logo_box_x,
                    logo_box_y,
                    logo_box_width,
                    logo_box_height,
                    panel_radius,
                    fill=1,
                    stroke=0,
                )

                # marges internes plus confortables
                padding_x = 3.0 * mm
                padding_y = 1.4 * mm
                available_w = logo_box_width - (2 * padding_x)
                available_h = logo_box_height - (2 * padding_y)

                scale = min(available_w / image_width, available_h / image_height)

                # sécurité supplémentaire pour éviter tout débordement
                scale *= 0.94

                draw_w = image_width * scale
                draw_h = image_height * scale

                image_x = logo_box_x + (logo_box_width - draw_w) / 2
                image_y = logo_box_y + (logo_box_height - draw_h) / 2

                canv.drawImage(
                    reader,
                    image_x,
                    image_y,
                    width=draw_w,
                    height=draw_h,
                    mask="auto",
                    preserveAspectRatio=True,
                    anchor="sw",
                )
            left_text_x = 8 * mm
            left_current_top = height - 22 * mm
            left_current_top -= draw_paragraph('Costing<br/>Feasibility<br/>Template', hero_title_style, left_text_x, left_current_top, left_width - 16 * mm)
            left_current_top -= 5 * mm
            draw_paragraph('Structured RFQ snapshot prepared for the costing review phase.', hero_body_style, left_text_x, left_current_top, left_width - 16 * mm)

            right_text_x = right_x + 6.5 * mm
            right_content_width = width - right_text_x - 8 * mm

            current_top = height - 8.5 * mm
            current_top -= draw_paragraph(
                'REVIEW SNAPSHOT',
                content_kicker_style,
                right_text_x,
                current_top,
                right_content_width,
            )
            current_top -= 1.5 * mm

            current_top -= draw_paragraph(
                'Commercial-to-costing handoff',
                content_title_style,
                right_text_x,
                current_top,
                right_content_width,
            )
            current_top -= 3 * mm


            # Les badges se placent maintenant juste sous le texte
            pills_y = current_top - 10 * mm
            cards_stack_height = card_height + ((card_rows - 1) * (card_height + row_gap))
            bottom_cards_y = max(5.0 * mm, pills_y - (7.0 * mm + cards_stack_height))
            top_cards_y = bottom_cards_y + ((card_rows - 1) * (card_height + row_gap))

            draw_pill(
                f'<b>Phase :</b> {escape(phase)}',
                right_text_x,
                pills_y,
                34 * mm,
                colors.HexColor('#315f88'),
                sun,
            )
            draw_pill(f'<b>Sub-status :</b> {escape(sub_status)}', right_text_x + 37 * mm, pills_y, 41 * mm, colors.HexColor('#3f78b0'), colors.HexColor('#76a8d6'))
            for idx, (label, value) in enumerate(meta_cards_rl):
                row = idx // 3
                col = idx % 3
                x = cards_x + col * (card_width + card_gap)
                y = top_cards_y - row * (card_height + row_gap)
                draw_card(label, value, x, y)

            canv.restoreState()

    story.extend([ReportLabHeroHeader(), Spacer(1, 10)])

    story.append(Spacer(1, 10))

    rl_products_info = _build_products_info_rows(data)
    rl_volumes_rows = _build_volumes_rows(data)

    _ref_table_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), mint),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fbfd")]),
        ("BOX", (0, 0), (-1, -1), 0.8, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#eef3f7")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])

    def _make_ref_cell(value: str) -> Paragraph:
        return Paragraph(
            _paragraph_html(value),
            reference_empty_style if value == "-" else reference_value_style,
        )

    for index, (title, fields) in enumerate(FIELD_GROUPS):
        accent, head_bg, _, _ = SECTION_ACCENTS[index % len(SECTION_ACCENTS)]
        accent_color = colors.HexColor(accent)
        heading_bg = colors.HexColor(head_bg)

        header = Table(
            [[Paragraph(f"{index + 1}", section_number_style), Paragraph(escape(title), section_title_style)]],
            colWidths=[14 * mm, 166 * mm],
        )
        header.setStyle(TableStyle([
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
        ]))
        story.append(header)

        if title == "Products":
            rl_rows = [
                [Paragraph(h, reference_header_style) for h in
                 ("#", "Product", "Product line", "Costing data", "Application", "Part number", "SOP year")]
            ]
            for row in rl_products_info:
                rl_rows.append([
                    Paragraph(escape(row["index"]), reference_value_style),
                    _make_ref_cell(row["product"]),
                    _make_ref_cell(row["product_line"]),
                    _make_ref_cell(row["costing_data"]),
                    _make_ref_cell(row["application"]),
                    _make_ref_cell(row["part_number"]),
                    _make_ref_cell(row["sop_year"]),
                ])
            if len(rl_rows) > 1:
                # 8+36+28+32+28+28+20 = 180mm (matches section header width)
                t = Table(rl_rows, colWidths=[8*mm, 36*mm, 28*mm, 32*mm, 28*mm, 28*mm, 20*mm], repeatRows=1)
                t.setStyle(_ref_table_style)
                story.extend([Spacer(1, 4), t])

        elif title == "Volumes":
            rl_rows = [
                [Paragraph(h, reference_header_style) for h in
                 ("#", "Part number", "Revision level", "Qty / year", "Target price", "Currency", "Price source", "Delivery zone", "Delivery plant", "Country")]
            ]
            for row in rl_volumes_rows:
                rl_rows.append([
                    Paragraph(escape(row["index"]), reference_value_style),
                    _make_ref_cell(row["part_number"]),
                    _make_ref_cell(row["revision_level"]),
                    _make_ref_cell(row["quantity"]),
                    _make_ref_cell(row["target_price"]),
                    _make_ref_cell(row["currency"]),
                    _make_ref_cell(row["price_source"]),
                    _make_ref_cell(row["delivery_zone"]),
                    _make_ref_cell(row["plant"]),
                    _make_ref_cell(row["country"]),
                ])
            if len(rl_rows) > 1:
                # 8+23+13+25+21+14+20+20+18+18 = 180mm (matches section header width)
                t = Table(rl_rows, colWidths=[8*mm, 23*mm, 13*mm, 25*mm, 21*mm, 14*mm, 20*mm, 20*mm, 18*mm, 18*mm], repeatRows=1)
                t.setStyle(_ref_table_style)
                story.extend([Spacer(1, 4), t])

        else:
            field_rows = []
            for label, keys in fields:
                display_value = _get_field_display_value(label, keys, data)
                value_style = field_empty_style if display_value == "-" else field_value_style
                field_rows.append([
                    Paragraph(escape(label.upper()), field_label_style),
                    Paragraph(_paragraph_html(display_value), value_style),
                ])
            body = Table(field_rows, colWidths=[56 * mm, 124 * mm])
            body.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.8, border_color),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#eef3f7")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(body)

        story.append(Spacer(1, 10))

    note = Table(
        [[Paragraph("<b>Note:</b> Empty fields are displayed as &mdash;. This document is generated automatically from the RFQ system and does not constitute a contractual commitment.", note_style)]],
        colWidths=[180 * mm],
    )
    note.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbf5")),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#f5d9a8")),
        ("LINEBEFORE", (0, 0), (0, 0), 3, sun),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.extend([note, Spacer(1, 10)])

    document.build(story)
    return buffer.getvalue()


# ── HTML helpers ─────────────────────────────────────────────────────────────


def _render_field_group(
    title: str,
    fields: list[tuple[str, tuple[str, ...]]],
    data: dict[str, Any],
    index: int,
    *,
    products_info_rows: list[dict[str, str]] | None = None,
    volumes_rows_data: list[dict[str, str]] | None = None,
) -> str:
    accent, head_bg, num_bg, num_color = SECTION_ACCENTS[index % len(SECTION_ACCENTS)]
    section_number = index + 1
    border_color = accent + "33"

    if title == "Products":
        rows_data = products_info_rows if products_info_rows is not None else _build_products_info_rows(data)
        section_body = f'<div class="section-body">{_render_products_info_table_html(rows_data)}</div>'
    elif title == "Volumes":
        rows_data_v = volumes_rows_data if volumes_rows_data is not None else _build_volumes_rows(data)
        section_body = f'<div class="section-body">{_render_volumes_table_html(rows_data_v)}</div>'
    else:
        rows: list[str] = []
        for label, keys in fields:
            display_value = _get_field_display_value(label, keys, data)
            is_empty = display_value == "-"
            cell_class = "value-empty" if is_empty else "value"
            cell_content = "&mdash;" if is_empty else _format_html_value(display_value)
            rows.append(
                f'<tr>'
                f'<td class="label">{escape(label)}</td>'
                f'<td class="{cell_class}">{cell_content}</td>'
                f'</tr>'
            )
        section_body = f'<div class="section-body"><table class="fields">{"".join(rows)}</table></div>'

    return f"""
    <div class="section">
      <div class="section-top" style="background:{accent};"></div>
      <div class="section-head" style="background:{head_bg}; border-bottom-color:{border_color};">
        <div class="section-head-inner">
          <span class="section-num" style="background:{num_bg}; color:{num_color};">{section_number}</span>
          <h2>{escape(title)}</h2>
        </div>
      </div>
      {section_body}
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


def _render_product_reference_table_html(product_rows: list[dict[str, str]]) -> str:
    table_rows = "".join(
        (
            "<tr>"
            f'<td class="reference-index">{escape(row["index"])}</td>'
            f"<td>{escape(row['part_number'])}</td>"
            f"<td>{escape(row['revision_level'])}</td>"
            f"<td>{escape(row['quantity'])}</td>"
            f"<td>{escape(row['target_price_display'])}</td>"
            f"<td>{escape(row['price_source'])}</td>"
            f"<td>{escape(row['target_to_display'])}</td>"
            f"<td>{escape(row.get('delivery_zone', '-'))}</td>"
            f"<td>{escape(row.get('plant', '-'))}</td>"
            f"<td>{escape(row.get('country', '-'))}</td>"
            "</tr>"
        )
        for row in product_rows
    )

    return f"""
    <p class="section-copy">Products</p>
    <table class="reference-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Part number</th>
          <th>Revision</th>
          <th>Qty/year</th>
          <th>Target price</th>
          <th>Price source</th>
          <th>Target TO</th>
          <th>Delivery Zone</th>
          <th>Plant</th>
          <th>Country</th>
        </tr>
      </thead>
      <tbody>{table_rows}</tbody>
    </table>
    """


def _render_products_info_table_html(rows: list[dict[str, str]]) -> str:
    if not rows:
        return '<p style="font-style:italic;color:#b8c9d6;font-size:10pt;margin:6pt 0;">No products recorded.</p>'
    table_rows = "".join(
        f"<tr>"
        f'<td class="reference-index">{escape(row["index"])}</td>'
        f"<td>{escape(row['product'])}</td>"
        f"<td>{escape(row['product_line'])}</td>"
        f"<td>{escape(row['costing_data'])}</td>"
        f"<td>{escape(row['application'])}</td>"
        f"<td>{escape(row['part_number'])}</td>"
        f"<td>{escape(row['sop_year'])}</td>"
        f"</tr>"
        for row in rows
    )
    return (
        '<table class="reference-table">'
        "<thead><tr>"
        "<th>#</th><th>Product</th><th>Product line</th><th>Costing data</th>"
        "<th>Application</th><th>Part number</th><th>SOP year</th>"
        "</tr></thead>"
        f"<tbody>{table_rows}</tbody>"
        "</table>"
    )


def _render_volumes_table_html(rows: list[dict[str, str]]) -> str:
    if not rows:
        return '<p style="font-style:italic;color:#b8c9d6;font-size:10pt;margin:6pt 0;">No volumes data recorded.</p>'
    table_rows = "".join(
        f"<tr>"
        f'<td class="reference-index">{escape(row["index"])}</td>'
        f"<td>{escape(row['part_number'])}</td>"
        f"<td>{escape(row['revision_level'])}</td>"
        f"<td>{escape(row['quantity'])}</td>"
        f"<td>{escape(row['target_price'])}</td>"
        f"<td>{escape(row['currency'])}</td>"
        f"<td>{escape(row['price_source'])}</td>"
        f"<td>{escape(row['delivery_zone'])}</td>"
        f"<td>{escape(row['plant'])}</td>"
        f"<td>{escape(row['country'])}</td>"
        f"</tr>"
        for row in rows
    )
    return (
        '<table class="reference-table">'
        "<thead><tr>"
        "<th>#</th><th>Part number</th><th>Revision level</th><th>Qty / year</th>"
        "<th>Target price</th><th>Currency</th><th>Price source</th>"
        "<th>Delivery zone</th><th>Delivery plant</th><th>Country</th>"
        "</tr></thead>"
        f"<tbody>{table_rows}</tbody>"
        "</table>"
    )


def _render_logo_html() -> str:
    data_uri = _load_logo_data_uri()
    if not data_uri:
        return ""
    return f'<div class="logo-wrap"><img src="{data_uri}" alt="Avo Carbon Group" /></div>'


def _resolve_logo_path() -> Path | None:
    for candidate in LOGO_CANDIDATE_PATHS:
        if candidate.exists():
            return candidate
    return None


@lru_cache(maxsize=1)
def _load_logo_data_uri() -> str:
    logo_path = _resolve_logo_path()
    if not logo_path:
        return ""
    try:
        content = logo_path.read_bytes()
    except OSError:
        return ""
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# ── Data helpers ─────────────────────────────────────────────────────────────


def _get_field_display_value(
    label: str,
    keys: tuple[str, ...],
    data: dict[str, Any],
    *,
    product_rows: list[dict[str, str]] | None = None,
) -> str:
    if label == "Total Turnover":
        return _format_total_turnover_display(data)
    multi_reference_display = _build_multi_reference_field_display(
        label,
        product_rows if product_rows is not None else _build_product_reference_rows(data),
    )
    if multi_reference_display is not None:
        return multi_reference_display
    if label == "Target price":
        return _format_target_price_display(data)
    value = _pick_first_value(data, keys)
    if value and value != "-" and "email" not in label.lower():
        value = value[0].upper() + value[1:]
    return value


def _format_target_price_display(data: dict[str, Any]) -> str:
    shared_currency = _resolve_shared_product_currency(data)
    local_value = _pick_first_raw_value(data, ("target_price_local", "targetPriceLocal"))
    eur_value = _pick_first_raw_value(data, ("target_price_eur", "targetPriceEur", "targetPrice"))
    if local_value is None:
        first_product = data.get("products")[0] if isinstance(data.get("products"), list) and data.get("products") else {}
        if isinstance(first_product, dict):
            local_value = _pick_first_raw_value(first_product, ("target_price", "targetPrice", "price"))
            if eur_value is None and shared_currency == "EUR":
                eur_value = local_value

    return _format_dual_currency_price_display(
        local_value,
        shared_currency,
        eur_value=eur_value,
        eur_rate=_derive_eur_rate(data, shared_currency),
    )

def _build_products_info_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    """Rows for the Products table: product name, line, application, part number, costing data, SOP year."""
    products = data.get("products")
    if not isinstance(products, list):
        return []
    rows: list[dict[str, str]] = []
    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            continue
        rows.append({
            "index": str(index),
            "product": _pick_first_value(product, ("product", "product_name", "productName")),
            "product_line": _pick_first_value(
                product,
                ("product_line", "product_line_acronym", "productLine", "productLineAcronym"),
            ),
            "application": (
                _pick_first_value(product, ("application",))
                or _pick_first_value(data, ("application",))
            ),
            "part_number": _pick_first_value(product, ("part_number", "partNumber", "customer_pn")),
            "costing_data": (
                _pick_first_value(product, ("costing_data", "costingData"))
                or _pick_first_value(data, ("costing_data", "costingData"))
            ),
            "sop_year": _pick_first_value(product, ("sop", "sop_year", "sopYear")),
        })
    return rows


def _build_volumes_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    """Rows for the Volumes table: per-product quantities, prices, and delivery info."""
    products = data.get("products")
    if not isinstance(products, list):
        return []
    volumes_list = data.get("volumes")
    if not isinstance(volumes_list, list):
        volumes_list = []
    shared_currency = _resolve_shared_product_currency(data)
    rows: list[dict[str, str]] = []
    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            continue
        volume = (
            volumes_list[index - 1]
            if index - 1 < len(volumes_list) and isinstance(volumes_list[index - 1], dict)
            else {}
        )
        yearly_volumes: dict[str, Any] = volume.get("volumes") or {}
        if yearly_volumes and isinstance(yearly_volumes, dict):
            total_qty = sum(v for v in yearly_volumes.values() if isinstance(v, (int, float)))
            if len(yearly_volumes) == 1:
                year_key = next(iter(yearly_volumes))
                quantity_display = f"{_format_numeric_display(total_qty)} ({year_key})"
            else:
                quantity_display = "\n".join(
                    f"{k}: {_format_numeric_display(v)}" for k, v in sorted(yearly_volumes.items())
                )
        else:
            qty_raw = _pick_first_raw_value(
                product, ("quantity", "qty", "annual_volume", "annualVolume")
            )
            quantity_display = _format_numeric_display(qty_raw)
        target_price_raw = (
            volume.get("target_price")
            if volume.get("target_price") is not None
            else _pick_first_raw_value(product, ("target_price", "targetPrice", "price"))
        )
        row_currency = (
            _stringify_value(
                _pick_first_raw_value(
                    product,
                    ("currency", "target_price_currency", "targetPriceCurrency"),
                )
            ).strip().upper()
            or shared_currency
        )
        price_source_raw = (
            volume.get("price_source")
            if volume.get("price_source") is not None
            else _pick_first_raw_value(
                product,
                ("target_price_is_estimated", "targetPriceIsEstimated", "price_source"),
            )
        )
        rows.append({
            "index": str(index),
            "part_number": _pick_first_value(product, ("part_number", "partNumber", "customer_pn")),
            "revision_level": _pick_first_value(
                product, ("revision_level", "revisionLevel", "revision")
            ),
            "quantity": quantity_display,
            "target_price": _format_currency_amount_display(target_price_raw, row_currency),
            "currency": row_currency or "-",
            "price_source": _format_price_source_display(price_source_raw),
            "delivery_zone": _stringify_value(volume.get("delivery_zone")) or "-",
            "plant": _stringify_value(volume.get("plant")) or "-",
            "country": _stringify_value(volume.get("country")) or "-",
        })
    return rows


def _build_product_reference_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    products = data.get("products")
    if not isinstance(products, list):
        return []

    volumes_list = data.get("volumes")
    if not isinstance(volumes_list, list):
        volumes_list = []

    shared_currency = _resolve_shared_product_currency(data)
    eur_rate = _derive_eur_rate(data, shared_currency)
    rows: list[dict[str, str]] = []
    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            continue

        volume = volumes_list[index - 1] if index - 1 < len(volumes_list) and isinstance(volumes_list[index - 1], dict) else {}

        # Qty: prefer yearly breakdown from volumes[i].volumes dict; fallback to products[i].quantity
        yearly_volumes: dict[str, Any] = volume.get("volumes") or {}
        if yearly_volumes and isinstance(yearly_volumes, dict):
            total_qty = sum(v for v in yearly_volumes.values() if isinstance(v, (int, float)))
            if len(yearly_volumes) == 1:
                year_key = next(iter(yearly_volumes))
                quantity_display = f"{_format_numeric_display(total_qty)} ({year_key})"
            else:
                lines = " / ".join(f"{k}: {_format_numeric_display(v)}" for k, v in sorted(yearly_volumes.items()))
                quantity_display = lines
            quantity_number = total_qty
        else:
            quantity_raw = _pick_first_raw_value(
                product,
                ("quantity", "qty", "annual_volume", "annualVolume", "qty_per_year", "qtyPerYear"),
            )
            quantity_display = _format_numeric_display(quantity_raw)
            quantity_number = _coerce_float_value(quantity_raw)

        # Target price: prefer volumes[i].target_price, fallback to products[i].target_price
        target_price_raw = volume.get("target_price") if volume.get("target_price") is not None else _pick_first_raw_value(product, ("target_price", "targetPrice", "price"))

        row_currency = _stringify_value(
            _pick_first_raw_value(
                product,
                ("currency", "target_price_currency", "targetPriceCurrency", "target_currency", "targetCurrency"),
            )
        ).strip().upper() or shared_currency

        # Price source: prefer volumes[i].price_source (already a string), fallback to products[i].target_price_is_estimated
        price_source_raw = volume.get("price_source") if volume.get("price_source") is not None else _pick_first_raw_value(
            product,
            ("target_price_is_estimated", "targetPriceIsEstimated", "price_source", "priceSource"),
        )

        target_price_number = _coerce_float_value(target_price_raw)
        target_to_raw = _pick_first_raw_value(product, ("target_to", "targetTo", "turnover"))
        if target_to_raw is None and quantity_number is not None and target_price_number is not None:
            target_to_raw = quantity_number * target_price_number

        target_price_display = _format_dual_currency_price_display(
            target_price_raw,
            row_currency,
            eur_rate=eur_rate if row_currency == shared_currency else None,
        )
        price_source_display = _format_price_source_display(price_source_raw)
        target_to_display = _format_dual_currency_turnover_display(
            target_to_raw,
            row_currency,
            eur_rate=eur_rate if row_currency == shared_currency else None,
        )

        rows.append(
            {
                "index": str(index),
                "part_number": _pick_first_value(product, ("part_number", "partNumber", "customer_pn", "customerPn")),
                "revision_level": _pick_first_value(product, ("revision_level", "revisionLevel", "revision")),
                "quantity": quantity_display,
                "target_price_display": target_price_display,
                "price_source": price_source_display,
                "target_price_summary": target_price_display,
                "target_to_display": target_to_display,
                "delivery_zone": _stringify_value(volume.get("delivery_zone")) or "-",
                "plant": _stringify_value(volume.get("plant")) or "-",
                "country": _stringify_value(volume.get("country")) or "-",
            }
        )

    return rows


def _build_multi_reference_field_display(
    label: str,
    product_rows: list[dict[str, str]],
) -> str | None:
    if len(product_rows) <= 1:
        return None

    field_name = {
        "Customer PN": "part_number",
        "Revision level": "revision_level",
        "Quantity per year": "quantity",
        "Target price": "target_price_summary",
    }.get(label)
    if not field_name:
        return None

    return "\n".join(f"{row['index']}. {row[field_name]}" for row in product_rows)


def _resolve_shared_product_currency(data: dict[str, Any]) -> str:
    products = data.get("products")
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            currency = _stringify_value(
                _pick_first_raw_value(
                    product,
                    ("currency", "target_price_currency", "targetPriceCurrency", "target_currency", "targetCurrency"),
                )
            ).strip().upper()
            if currency:
                return currency
    return _stringify_value(
        _pick_first_raw_value(data, ("target_price_currency", "targetPriceCurrency"))
    ).strip().upper()


def _derive_eur_rate(data: dict[str, Any], currency: str | None) -> float | None:
    normalized_currency = str(currency or "").strip().upper()
    if not normalized_currency:
        return None
    if normalized_currency == "EUR":
        return 1.0

    local_k_value = _coerce_float_value(_pick_first_raw_value(data, ("to_total_local", "toTotalLocal")))
    eur_k_value = _coerce_float_value(_pick_first_raw_value(data, ("to_total", "toTotal")))
    if local_k_value not in (None, 0.0) and eur_k_value is not None:
        if not _floats_close(local_k_value, eur_k_value):
            return eur_k_value / local_k_value

    local_price_value = _coerce_float_value(_pick_first_raw_value(data, ("target_price_local", "targetPriceLocal")))
    eur_price_value = _coerce_float_value(_pick_first_raw_value(data, ("target_price_eur", "targetPriceEur", "targetPrice")))
    if local_price_value not in (None, 0.0) and eur_price_value is not None:
        return eur_price_value / local_price_value

    return None


def _floats_close(left: float, right: float, tolerance: float = 1e-9) -> bool:
    return abs(left - right) <= tolerance


def _format_dual_currency_price_display(
    local_value: Any,
    currency: str | None,
    *,
    eur_value: Any | None = None,
    eur_rate: float | None = None,
) -> str:
    normalized_currency = str(currency or "").strip().upper() or "EUR"
    local_segment = _format_currency_amount_display(local_value, normalized_currency)
    if local_segment == "-":
        if normalized_currency == "EUR":
            return _format_currency_amount_display(eur_value, "EUR")
        return "-"
    if normalized_currency == "EUR":
        return local_segment

    eur_number = _coerce_float_value(eur_value)
    if eur_number is None and eur_rate is not None:
        local_number = _coerce_float_value(local_value)
        if local_number is not None:
            eur_number = local_number * eur_rate
    eur_segment = _format_currency_amount_display(eur_number, "EUR") if eur_number is not None else "EUR unavailable"
    return f"{local_segment} / {eur_segment}"


def _format_dual_currency_turnover_display(
    local_value: Any,
    currency: str | None,
    *,
    eur_k_value: Any | None = None,
    eur_rate: float | None = None,
) -> str:
    normalized_currency = str(currency or "").strip().upper() or "EUR"
    local_segment = _format_target_to_display(local_value, normalized_currency)
    if local_segment == "-":
        if normalized_currency == "EUR":
            return _format_k_amount_display(eur_k_value, "EUR")
        return "-"
    if normalized_currency == "EUR":
        return local_segment

    eur_k_number = _coerce_float_value(eur_k_value)
    if eur_k_number is None and eur_rate is not None:
        local_number = _coerce_float_value(local_value)
        if local_number is not None:
            eur_k_number = (local_number * eur_rate) / 1000.0
    eur_segment = _format_k_amount_display(eur_k_number, "EUR") if eur_k_number is not None else "EUR unavailable"
    return f"{local_segment} / {eur_segment}"


def _format_total_turnover_display(data: dict[str, Any]) -> str:
    shared_currency = _resolve_shared_product_currency(data) or "EUR"
    eur_rate = _derive_eur_rate(data, shared_currency)
    total_local_value = _coerce_float_value(_pick_first_raw_value(data, ("total_target_to",)))
    if total_local_value is None:
        total_local_k = _coerce_float_value(_pick_first_raw_value(data, ("to_total_local", "toTotalLocal")))
        if total_local_k is not None:
            total_local_value = total_local_k * 1000.0

    total_eur_k_value = _coerce_float_value(_pick_first_raw_value(data, ("to_total", "toTotal")))
    if shared_currency != "EUR" and eur_rate is None:
        total_eur_k_value = None
    return _format_dual_currency_turnover_display(
        total_local_value,
        shared_currency,
        eur_k_value=total_eur_k_value,
        eur_rate=eur_rate,
    )


def _pick_first_raw_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if _has_meaningful_value(value):
            return value
    return None


def _pick_first_value(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    value = _pick_first_raw_value(data, keys)
    if value is None:
        return "-"
    return _stringify_value(value)


def _coerce_float_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _format_numeric_display(value: Any) -> str:
    number = _coerce_float_value(value)
    if number is None:
        return "-"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.5f}".rstrip("0").rstrip(".")


def _format_currency_amount_display(value: Any, currency: str | None) -> str:
    amount = _format_numeric_display(value)
    if amount == "-":
        return "-"
    return f"{amount} {currency}".strip()


def _format_k_amount_display(value: Any, currency: str | None) -> str:
    amount = _format_numeric_display(value)
    if amount == "-":
        return "-"
    unit = f"k{currency}" if currency else "k"
    return f"{amount} {unit}".strip()


def _format_target_to_display(value: Any, currency: str | None) -> str:
    number = _coerce_float_value(value)
    if number is None:
        return "-"
    return _format_k_amount_display(number / 1000.0, currency)


def _format_price_source_display(value: Any) -> str:
    if value is None:
        return "-"
    text = _stringify_value(value).strip()
    if not text:
        return "-"
    lower = text.lower()
    if lower == "estimated" or lower == "true" or lower == "1" or lower == "yes":
        return "Estimated"
    if lower in ("official customer price", "official", "false", "0", "no"):
        return "Official Customer Price"
    return text


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes"}


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
