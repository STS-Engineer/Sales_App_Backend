from __future__ import annotations

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

_BROWSER_CANDIDATE_PATHS = (
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def build_costing_template_filename(rfq: Rfq) -> str:
    data = dict(rfq.rfq_data or {})
    base_name = _stringify_value(data.get("systematic_rfq_id") or rfq.rfq_id)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._-") or "rfq"
    return f"{safe_name}_costing_feasibility_template.pdf"


def render_costing_template_pdf(rfq: Rfq) -> bytes:
    document_html = render_costing_template_html(rfq)
    return _render_html_to_pdf(document_html)


def render_costing_template_html(rfq: Rfq) -> str:
    data = dict(rfq.rfq_data or {})
    if rfq.zone_manager_email and not _has_meaningful_value(data.get("zone_manager_email")):
        data["zone_manager_email"] = rfq.zone_manager_email
    if rfq.product_line_acronym and not _has_meaningful_value(data.get("product_line_acronym")):
        data["product_line_acronym"] = rfq.product_line_acronym

    systematic_rfq_id = _stringify_value(data.get("systematic_rfq_id") or "Pending assignment")
    approved_by = _stringify_value(data.get("zone_manager_email")) or "-"
    approval_date = _stringify_value(rfq.approved_at) or "-"
    generated_at = _stringify_value(datetime.now(timezone.utc))

    sections_html = "".join(
        _render_field_group(title, fields, data) for title, fields in FIELD_GROUPS
    )

    return f"""<!DOCTYPE html>
<html xmlns:o="urn:schemas-microsoft-com:office:office"
      xmlns:w="urn:schemas-microsoft-com:office:word"
      xmlns="http://www.w3.org/TR/REC-html40">
<head>
  <meta charset="utf-8" />
  <title>RFQ Data</title>
  <style>
    @page {{
      size: A4;
      margin: 42pt 42pt 42pt 42pt;
    }}
    body {{
      font-family: Calibri, Arial, sans-serif;
      color: #1f2937;
      font-size: 11pt;
      line-height: 1.45;
      margin: 0;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    h1 {{
      color: #0f3b64;
      font-size: 22pt;
      margin: 0 0 8pt 0;
    }}
    h2 {{
      color: #0f3b64;
      font-size: 14pt;
      margin: 18pt 0 8pt 0;
      padding: 6pt 8pt;
      background: #eaf2f9;
      border: 1pt solid #c7d8e6;
    }}
    p.meta {{
      margin: 0 0 4pt 0;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      margin-bottom: 8pt;
    }}
    td {{
      border: 1pt solid #d8e1ea;
      padding: 7pt 8pt;
      vertical-align: top;
    }}
    td.label {{
      width: 34%;
      font-weight: bold;
      background: #f8fafc;
    }}
    .note {{
      margin-top: 16pt;
      padding: 10pt 12pt;
      background: #fff7ed;
      border: 1pt solid #fed7aa;
    }}
  </style>
</head>
<body>
  <div>
    <h1>RFQ Data</h1>
    <p class="meta"><strong>RFQ ID:</strong> {_format_html_value(systematic_rfq_id)}</p>
    <p class="meta"><strong>Current phase:</strong> {_format_html_value(rfq.phase.value)}</p>
    <p class="meta"><strong>Current sub-status:</strong> {_format_html_value(rfq.sub_status.value)}</p>
    <p class="meta"><strong>Created by:</strong> {_format_html_value(rfq.created_by_email)}</p>
    <p class="meta"><strong>Approved by:</strong> {_format_html_value(approved_by)}</p>
    <p class="meta"><strong>Approved at:</strong> {_format_html_value(approval_date)}</p>
    <p class="meta"><strong>Generated at:</strong> {_format_html_value(generated_at)}</p>

    {sections_html}

    <div class="note">
      This document is generated from the New RFQ form at the moment the RFQ enters the
      costing feasibility workflow. Empty fields are shown as "-".
    </div>
  </div>
</body>
</html>
"""


def _render_html_to_pdf(document_html: str) -> bytes:
    browser_path = _find_browser_executable()
    if browser_path is None:
        raise RuntimeError(
            "No compatible browser was found for PDF generation. Install Microsoft Edge or Google Chrome."
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
            raise RuntimeError(f"PDF generation failed: {stderr or 'browser render error'}") from exc
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
) -> str:
    rows = "".join(
        (
            f"<tr><td class=\"label\">{escape(label)}</td>"
            f"<td>{_format_html_value(_pick_first_value(data, keys))}</td></tr>"
        )
        for label, keys in fields
    )
    return f"<h2>{escape(title)}</h2><table>{rows}</table>"


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
    if not text:
        text = "-"
    return escape(text).replace("\n", "<br />")
