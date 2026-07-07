"""
AI pre-validation service for Workspace Agents.

Sends normalized RFQ data to a published Workspace Agent API channel before
the human validator receives the email.

Environment variables:
    AGENT_ACCESS_TOKEN
        Bearer token for the Workspace Agents API.
    WORKSPACE_AGENT_TRIGGER_ID
        Published API channel trigger ID in agtch_... format.
"""

import datetime
import json
import logging
import re
import base64
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.services.notifications import EMAIL_VALIDATION_REQUEST, record_notification_sent
from app.utils import emails

logger = logging.getLogger(__name__)

# ── Discussion text parser ────────────────────────────────────────────────────
# Detects the agent's triage decision from its French/English free-text output.
# Checked in order: explicit "Statut:" line → negated release → positive release.

_BLOCKED_STATUS_WORDS = ("bloqué", "blocked", "rejeté", "rejected", "attente", "waiting")
_APPROVED_STATUS_WORDS = ("libéré", "released", "ok pour", "validé", "approved", "approuvé")

_BLOCKED_PHRASES = (
    "ne peut pas être libéré",
    "cannot be released",
    "informations obligatoires",
    "informations manquantes",
    "bloquant v1",
    "bloqué en attente",
    "dossier bloqué",
)
_APPROVED_PHRASES = (
    "libéré vers le chiffrage",
    "released for costing",
    "ok pour le chiffrage",
    "validé pour chiffrage",
    "dossier complet",
    "can be released",
    "peut être libéré",
)


def infer_approved_from_discussion(text: str) -> bool | None:
    """Return True/False/None by scanning the agent's discussion for a triage decision.

    Priority:
    1. Explicit "Statut : <word>" line (most reliable).
    2. Specific negated / positive release phrases.
    3. Returns None when the text is ambiguous.
    """
    if not text:
        return None
    norm = text.lower()

    # 1. Look for an explicit "Statut : ..." line produced by the agent.
    status_match = re.search(r"statut\s*[:\-]\s*(.+?)(?:\n|$)", norm)
    if status_match:
        status_text = status_match.group(1).strip()
        if any(w in status_text for w in _BLOCKED_STATUS_WORDS):
            return False
        if any(w in status_text for w in _APPROVED_STATUS_WORDS):
            return True

    # 2. Specific decision phrases.
    if any(p in norm for p in _BLOCKED_PHRASES):
        return False
    if any(p in norm for p in _APPROVED_PHRASES):
        return True

    return None

_REQUEST_TIMEOUT_SECONDS = 500
_LEGACY_DEFAULT_TRIGGER_ID = "agtch_6a42944ed300819194b33fc75540665e"
_AI_VALIDATION_FINAL_STATUSES = {"completed", "skipped"}
_AI_VALIDATION_PENDING_STATUSES = {"queued", "processing"}
_AI_VALIDATION_STATUS_ALIASES = {
    "approved": ("completed", True),
    "accepted": ("completed", True),
    "rejected": ("completed", False),
    "reject": ("completed", False),
    "blocked": ("completed", False),
    "pending": ("processing", None),
    "in_progress": ("processing", None),
}


@dataclass
class AgentValidationResult:
    approved: bool
    message: str
    fields_to_correct: list[str] = field(default_factory=list)
    discussion: str = ""
    status: str = "completed"
    conversation_url: str = ""


def current_timestamp_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalize_ai_validation_status(
    status: str | None,
    approved: bool | None = None,
) -> str:
    raw_status = str(status or "").strip().lower()
    aliased = _AI_VALIDATION_STATUS_ALIASES.get(raw_status)
    if aliased:
        return aliased[0]
    if raw_status in _AI_VALIDATION_PENDING_STATUSES:
        return raw_status
    if raw_status in _AI_VALIDATION_FINAL_STATUSES:
        return raw_status
    if approved is None:
        return "processing"
    return "completed"


def resolve_ai_validation_approved(
    approved: bool | None,
    status: str | None,
    *,
    fallback: bool = True,
) -> bool:
    if isinstance(approved, bool):
        return approved
    raw_status = str(status or "").strip().lower()
    aliased = _AI_VALIDATION_STATUS_ALIASES.get(raw_status)
    if aliased and isinstance(aliased[1], bool):
        return aliased[1]
    return fallback


async def apply_ai_validation_verdict(
    db: AsyncSession,
    rfq: Rfq,
    *,
    approved: bool,
    send_email: bool = True,
) -> bool:
    """Resolve an RFQ sitting in PENDING_AI_APPROVAL once the agent's verdict is known.

    Approved -> moves to PENDING_FOR_VALIDATION and, unless the creator is also
    the assigned validator, emails the validator so they know it's their turn.
    Rejected -> moves to REJECTED_BY_AI; the creator must fix and resubmit.

    Returns True if a validator notification email was sent. No-ops (returns
    False) if the RFQ isn't currently awaiting an AI decision — e.g. a stale or
    duplicate callback arriving after the RFQ was already resolved.
    """
    if rfq.phase != RfqPhase.RFQ or rfq.sub_status != RfqSubStatus.PENDING_AI_APPROVAL:
        return False

    if not approved:
        rfq.sub_status = RfqSubStatus.REJECTED_BY_AI
        await db.commit()
        return False

    rfq.sub_status = RfqSubStatus.PENDING_FOR_VALIDATION
    await db.commit()
    await db.refresh(rfq)

    zone_manager_email = str(rfq.zone_manager_email or "").strip()
    creator_email = str(rfq.created_by_email or "").strip().lower()
    creator_is_validator = zone_manager_email.lower() == creator_email
    if not send_email or not zone_manager_email or creator_is_validator:
        return False

    rfq_data = rfq.rfq_data or {}
    systematic_rfq_id = str(rfq_data.get("systematic_rfq_id") or "").strip() or rfq.rfq_id
    acronym = str(rfq.product_line_acronym or "").strip()
    validator_role = str(rfq_data.get("validator_role") or "Validator").strip() or "Validator"
    frontend_url = str(settings.frontend_url or "").rstrip("/")
    rfq_link = f"{frontend_url}/rfqs/new?id={rfq.rfq_id}" if frontend_url else rfq.rfq_id

    email_sent = emails.send_validation_email(
        zone_manager_email,
        systematic_rfq_id,
        acronym,
        rfq_link,
        validator_role=validator_role,
    )
    if email_sent:
        # Rfq.last_notification_sent_at is a naive DateTime column — asyncpg
        # rejects a tz-aware value here.
        rfq.last_notification_sent_at = datetime.datetime.utcnow()
        await record_notification_sent(
            db,
            rfq_id=rfq.rfq_id,
            recipients=zone_manager_email,
            email_type=EMAIL_VALIDATION_REQUEST,
        )
        await db.commit()
    return email_sent


def build_ai_validation_record(
    *,
    approved: bool,
    status: str,
    message: str = "",
    discussion: str = "",
    fields_to_correct: list[str] | None = None,
    conversation_url: str = "",
    checked_at: str | None = None,
    source: str = "",
) -> dict[str, Any]:
    return {
        "approved": bool(approved),
        "status": normalize_ai_validation_status(status, approved),
        "message": str(message or ""),
        "discussion": str(discussion or ""),
        "conversation_url": str(conversation_url or "").strip(),
        "fields_to_correct": [
            str(field_name) for field_name in (fields_to_correct or []) if field_name
        ],
        "checked_at": str(checked_at or "").strip(),
        "source": str(source or "").strip(),
    }


def extract_ai_validation_record(rfq_data: dict | None) -> dict[str, Any] | None:
    if not isinstance(rfq_data, dict):
        return None
    raw = rfq_data.get("ai_validation")
    if not isinstance(raw, dict):
        return None
    approved = resolve_ai_validation_approved(
        raw.get("approved"),
        raw.get("status"),
        fallback=True,
    )
    return build_ai_validation_record(
        approved=approved,
        status=str(raw.get("status") or ""),
        message=str(raw.get("message") or ""),
        discussion=str(raw.get("discussion") or ""),
        fields_to_correct=list(raw.get("fields_to_correct") or []),
        conversation_url=str(raw.get("conversation_url") or ""),
        checked_at=str(raw.get("checked_at") or ""),
        source=str(raw.get("source") or ""),
    )


def _coerce_number_like(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace(" ", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _compact_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _numbers_match(left: float | None, right: float | None, *, tolerance: float = 0.5) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _sort_yearly_volume_items(yearly_volumes: dict[str, float]) -> list[tuple[str, float]]:
    def _sort_key(item: tuple[str, float]) -> tuple[int, int | str]:
        year = str(item[0]).strip()
        try:
            return (0, int(year))
        except ValueError:
            return (1, year)

    return sorted(yearly_volumes.items(), key=_sort_key)


def prepare_rfq_payload_for_agent(rfq_data: dict[str, Any]) -> dict[str, Any]:
    """Add agent-only quantity interpretation hints without changing saved RFQ data."""
    payload = dict(rfq_data or {})

    raw_products = payload.get("products")
    products = (
        [dict(item) if isinstance(item, dict) else item for item in raw_products]
        if isinstance(raw_products, list)
        else []
    )
    if products:
        payload["products"] = products

    raw_volumes = payload.get("volumes")
    volumes = (
        [dict(item) if isinstance(item, dict) else item for item in raw_volumes]
        if isinstance(raw_volumes, list)
        else []
    )
    if volumes:
        payload["volumes"] = volumes

    agent_volume_rows: list[dict[str, Any]] = []
    first_cumulative_total: float | None = None

    for index, product in enumerate(products):
        if not isinstance(product, dict):
            continue

        volume_row = volumes[index] if index < len(volumes) and isinstance(volumes[index], dict) else {}
        raw_yearly_profile = volume_row.get("volumes")
        if not isinstance(raw_yearly_profile, dict):
            continue

        normalized_yearly_profile: dict[str, float] = {}
        for year, raw_amount in raw_yearly_profile.items():
            amount = _coerce_number_like(raw_amount)
            if amount is None:
                continue
            normalized_yearly_profile[str(year)] = amount

        if not normalized_yearly_profile:
            continue

        sorted_yearly_items = _sort_yearly_volume_items(normalized_yearly_profile)
        yearly_profile = {
            year: _compact_number(amount)
            for year, amount in sorted_yearly_items
        }
        yearly_total = sum(amount for _, amount in sorted_yearly_items)
        product_quantity = _coerce_number_like(product.get("quantity"))
        multi_year_profile = len(sorted_yearly_items) > 1
        quantity_matches_yearly_total = _numbers_match(product_quantity, yearly_total)

        quantity_basis = "yearly_profile_only"
        if product_quantity is not None:
            quantity_basis = "explicit_product_quantity"
            if quantity_matches_yearly_total and multi_year_profile:
                quantity_basis = "cumulative_program_total_matching_yearly_profile"
            elif quantity_matches_yearly_total:
                quantity_basis = "single_year_quantity_matching_yearly_profile"

        summary: dict[str, Any] = {
            "product_index": index + 1,
            "part_number": str(product.get("part_number") or "").strip(),
            "yearly_profile": yearly_profile,
            "yearly_total": _compact_number(yearly_total),
            "first_listed_year": sorted_yearly_items[0][0],
            "first_listed_year_quantity": _compact_number(sorted_yearly_items[0][1]),
            "quantity_basis": quantity_basis,
        }
        if product_quantity is not None:
            summary["raw_product_quantity"] = _compact_number(product_quantity)

        product["agent_yearly_volume_profile"] = yearly_profile
        product["agent_yearly_total_quantity"] = _compact_number(yearly_total)
        product["agent_quantity_basis"] = quantity_basis

        if quantity_basis == "cumulative_program_total_matching_yearly_profile":
            summary["annual_volume_confirmation_required"] = False
            summary["agent_interpretation"] = (
                "The linked product quantity matches the sum of the listed years. "
                "Treat it as cumulative program volume across those years, not as a separate annual quantity."
            )
            product["agent_annual_volume_confirmation_required"] = False
            product["agent_quantity_interpretation"] = (
                "Cumulative program total across the listed yearly profile."
            )
            if first_cumulative_total is None:
                first_cumulative_total = yearly_total

        agent_volume_rows.append(summary)

    if agent_volume_rows:
        payload["agent_volume_rows"] = agent_volume_rows
        payload["agent_volume_guidance"] = (
            "Use agent_volume_rows as the authoritative quantity-interpretation helper. "
            "When quantity_basis is `cumulative_program_total_matching_yearly_profile`, "
            "the linked product quantity and any matching legacy top-level quantity mirror "
            "represent the cumulative total across the listed years for turnover calculation. "
            "Do not ask the KAM to confirm whether that matching sum is annual or cumulative. "
            "Only raise a blocking quantity issue when the yearly profile itself is missing, "
            "internally contradictory, or cannot be mapped to the product."
        )

    if first_cumulative_total is not None:
        legacy_quantity_mirrors: dict[str, int | float] = {}
        for key in ("annual_volume", "qty_per_year", "qtyPerYear"):
            raw_value = _coerce_number_like(payload.get(key))
            if _numbers_match(raw_value, first_cumulative_total):
                compact_value = _compact_number(raw_value)
                if compact_value is not None:
                    legacy_quantity_mirrors[key] = compact_value
                payload.pop(key, None)
        if legacy_quantity_mirrors:
            payload["agent_legacy_quantity_mirrors"] = legacy_quantity_mirrors

    return payload


def build_workspace_agent_input(rfq_data: dict[str, Any]) -> str:
    prepared_payload = prepare_rfq_payload_for_agent(rfq_data)
    preamble = (
        "IMPORTANT: You must always respond in English, regardless of the language of the data.\n\n"
        "Important interpretation rules for this RFQ JSON:\n"
        "- `agent_volume_rows` is a backend-generated helper for quantity interpretation.\n"
        "- When a row uses `cumulative_program_total_matching_yearly_profile`, the linked "
        "`products[*].quantity` and any removed legacy top-level quantity mirror represent "
        "the cumulative program total across the listed years for turnover calculation.\n"
        "- In that case, use the year-by-year profile as authoritative and do not ask the "
        "KAM to confirm whether the matching sum is annual or cumulative.\n"
        "- Only block on quantity when the yearly profile itself is missing, contradictory, "
        "or cannot be mapped to the product.\n\n"
        "RFQ JSON:\n"
    )
    return preamble + json.dumps(prepared_payload, ensure_ascii=False, default=str)


# ── PDF / file extraction for agent payload ───────────────────────────────────

_MAX_FILE_TEXT_CHARS = 8_000  # Truncate to keep agent token budget sane
_PDF_VISION_MAX_PAGES = 2     # Pages rendered for GPT-4 Vision fallback
_BLOB_DOWNLOAD_TIMEOUT = 30   # Seconds per file for Azure download


async def _analyze_pdf_with_vision(
    content: bytes, openai_api_key: str, filename: str = ""
) -> str:
    """Render the first PDF pages as PNG and describe with GPT-4 Vision."""
    try:
        import fitz
        from openai import AsyncOpenAI
    except ImportError:
        return ""

    images_b64: list[str] = []
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            for page_num, page in enumerate(doc):
                if page_num >= _PDF_VISION_MAX_PAGES:
                    break
                mat = fitz.Matrix(2.0, 2.0)  # ~144 DPI
                pix = page.get_pixmap(matrix=mat)
                images_b64.append(base64.b64encode(pix.tobytes("png")).decode())
    except Exception as exc:
        logger.debug("PDF→image render failed for %s: %s", filename, exc)
        return ""

    if not images_b64:
        return ""

    content_parts: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Technical drawing file: {filename}\n"
                "Extract all visible data: dimensions, tolerances, materials, "
                "surface treatments, part numbers, notes, title block. "
                "Return a structured summary."
            ),
        }
    ]
    for img_b64 in images_b64:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"},
        })

    try:
        client = AsyncOpenAI(api_key=openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": content_parts}],
            max_tokens=2000,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("GPT-4o Vision analysis failed for %s: %s", filename, exc)
        return ""


def _download_blob_sync(
    connection_string: str, container: str, blob_name: str
) -> bytes:
    from azure.storage.blob import BlobServiceClient
    service = BlobServiceClient.from_connection_string(connection_string)
    blob_client = service.get_container_client(container).get_blob_client(blob_name)
    return blob_client.download_blob().readall()


async def prepare_rfq_files_for_agent(
    rfq_files: list[dict],
    *,
    backend_base_url: str,
) -> list[dict]:
    """Expose stable attachment references for the Workspace Agent.

    The agent should inspect the original blob file via the MCP tool
      ok_text           — text layer extracted successfully by PyMuPDF
      ok_vision         — image PDF described by GPT-4 Vision
      vision_unavailable — image PDF, no OpenAI key configured
      vision_failed     — Vision API returned nothing useful
      download_failed   — blob unreachable (the only status that should block)
    """
    if not rfq_files:
        return rfq_files

    normalized_backend_base = str(backend_base_url or "").strip().rstrip("/")
    enriched: list[dict] = []
    for file_entry in rfq_files:
        f = dict(file_entry)
        file_id = str(f.get("id") or "").strip()
        blob_name = str(f.get("blob_name") or "").strip()
        filename = str(f.get("filename") or f.get("name") or blob_name).strip()

        if file_id and normalized_backend_base and not f.get("proxy_url"):
            f["proxy_url"] = f"{normalized_backend_base}/api/rfq/files/{file_id}/proxy"
        f["agent_file_url"] = str(f.get("proxy_url") or f.get("url") or "").strip()
        f["agent_file_strategy"] = (
            "Use the MCP tool analyze_rfq_blob_attachment_with_openai to inspect the "
            "original file instead of relying on pre-extracted text."
        )

        tool_arguments: dict[str, str] = {}
        if file_id:
            tool_arguments["file_id"] = file_id
        elif filename:
            tool_arguments["filename_contains"] = filename
        f["agent_file_tool"] = {
            "name": "analyze_rfq_blob_attachment_with_openai",
            "arguments": tool_arguments,
            "purpose": "Analyze the original blob attachment as an OpenAI input_file.",
        }
        f["agent_file_text_status"] = "mcp_openai_input_file"
        f["agent_file_text"] = ""

        logger.info(
            "[file-prep] %s → status=%s chars=%s",
            filename,
            f.get("agent_file_text_status"),
            len(f.get("agent_file_text") or ""),
        )
        enriched.append(f)

    return enriched


def _parse_agent_response(raw: dict, _raw_text: str = "") -> AgentValidationResult:
    """Recursively parse the agent API response into an AgentValidationResult."""
    if "approved" in raw:
        approved = bool(raw.get("approved", False))
        message = str(raw.get("message") or "")
        fields = [str(field_name) for field_name in (raw.get("fields_to_correct") or []) if field_name]
        # Prefer the full outer discussion when JSON is wrapped in free text.
        discussion = _raw_text or str(raw.get("discussion") or message)
        return AgentValidationResult(
            approved=approved,
            message=message,
            fields_to_correct=fields,
            discussion=discussion,
            conversation_url=str(raw.get("conversation_url") or "").strip(),
        )

    output = raw.get("output") or raw.get("result") or raw.get("text") or ""

    if isinstance(output, dict):
        return _parse_agent_response(output, _raw_text=_raw_text)

    output_str = str(output).strip()
    if output_str:
        fenced_stripped = output_str
        if output_str.startswith("```"):
            lines = output_str.splitlines()
            inner = "\n".join(line for line in lines if not line.startswith("```")).strip()
            if inner:
                fenced_stripped = inner
        try:
            parsed = json.loads(fenced_stripped)
            if isinstance(parsed, dict) and "approved" in parsed:
                return _parse_agent_response(parsed, _raw_text=output_str)
        except (json.JSONDecodeError, ValueError):
            pass

        return AgentValidationResult(
            approved=False,
            message=fenced_stripped,
            fields_to_correct=[],
            discussion=output_str,
        )

    logger.warning("Unrecognized agent response format; keys=%s", list(raw.keys()))
    return AgentValidationResult(
        approved=False,
        message=(
            "The AI agent returned an unrecognized response. "
            "Please contact support or retry."
        ),
        fields_to_correct=[],
        discussion="",
    )


def _get_runtime_settings() -> Settings:
    """Reload settings so updated .env values take effect without a process restart."""
    return Settings()


def _resolve_agent_trigger_id(runtime_settings: Settings) -> str:
    return runtime_settings.workspace_agent_trigger_id or _LEGACY_DEFAULT_TRIGGER_ID


def _resolve_agent_endpoint(runtime_settings: Settings, trigger_id: str) -> str:
    return runtime_settings.workspace_agent_endpoint or (
        f"{runtime_settings.workspace_agent_base_url}/workspace_agents/{trigger_id}/trigger"
    )


def _build_queued_result(response: httpx.Response) -> AgentValidationResult:
    conversation_url = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            conversation_url = str(body.get("conversation_url") or "").strip()
    except Exception:
        body = None

    discussion = (
        "Workspace Agent trigger accepted and queued. "
        "The Workspace Agents trigger API does not return a synchronous validation result."
    )

    return AgentValidationResult(
        approved=True,
        message="Workspace Agent trigger accepted and queued.",
        fields_to_correct=[],
        discussion=discussion,
        status="queued",
        conversation_url=conversation_url,
    )


async def validate_rfq_with_agent(rfq_data: dict) -> AgentValidationResult:
    """Send RFQ data to the Workspace Agent API channel for AI pre-validation."""
    runtime_settings = _get_runtime_settings()
    access_token = runtime_settings.agent_access_token
    if not access_token:
        logger.warning(
            "AGENT_ACCESS_TOKEN is not set; AI validation skipped for RFQ %s.",
            rfq_data.get("systematic_rfq_id") or "?",
        )
        return AgentValidationResult(
            approved=True,
            message="AI validation skipped: token not configured.",
            status="skipped",
        )

    trigger_id = _resolve_agent_trigger_id(runtime_settings)
    agent_endpoint = _resolve_agent_endpoint(runtime_settings, trigger_id)
    rfq_id_label = rfq_data.get("systematic_rfq_id") or rfq_data.get("rfq_id") or "?"

    logger.info(
        "Sending RFQ %s to Workspace Agent. Trigger ID: %s Endpoint: %s Token prefix: %s",
        rfq_id_label,
        trigger_id,
        agent_endpoint,
        access_token[:8] + "..." if len(access_token) > 8 else "(short)",
    )

    payload = {"input": build_workspace_agent_input(rfq_data)}
    conversation_key = str(
        rfq_data.get("systematic_rfq_id") or rfq_data.get("rfq_id") or ""
    ).strip()
    if conversation_key:
        payload["conversation_key"] = conversation_key
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(agent_endpoint, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        logger.error(
            "Workspace Agent timeout after %ss for RFQ %s: %s",
            _REQUEST_TIMEOUT_SECONDS,
            rfq_id_label,
            exc,
        )
        raise
    except httpx.ConnectError as exc:
        logger.error(
            "Workspace Agent connect error for RFQ %s - cannot reach %s: %s",
            rfq_id_label,
            agent_endpoint,
            exc,
        )
        raise

    logger.info(
        "Workspace Agent HTTP %s for RFQ %s. Response headers: %s",
        response.status_code,
        rfq_id_label,
        dict(response.headers),
    )

    if response.status_code == 202:
        return _build_queued_result(response)

    if response.status_code == 401:
        print(f"[AI] 401 Unauthorized. Body: {response.text[:500]}", flush=True)
        raise httpx.HTTPStatusError(
            "AI agent authentication failed (401) - use a ChatGPT Workspace Agent access token with the Workspace Agents scope, not OPENAI_API_KEY.",
            request=response.request,
            response=response,
        )
    if response.status_code == 403:
        print(f"[AI] 403 Forbidden. Body: {response.text[:500]}", flush=True)
        raise httpx.HTTPStatusError(
            "AI agent access denied (403) - the token is valid but cannot trigger the configured Workspace Agent. "
            "Create the token from ChatGPT Admin > Access tokens with the Workspace Agents scope, "
            "and make sure the token owner can run the published agent in the same workspace. "
            f"Trigger ID: {trigger_id}. Body: {response.text[:300]}",
            request=response.request,
            response=response,
        )
    if response.status_code == 429:
        print(f"[AI] 429 Rate limited. Body: {response.text[:500]}", flush=True)
        raise httpx.HTTPStatusError(
            "AI agent is temporarily unavailable (rate limit). Please retry in a moment.",
            request=response.request,
            response=response,
        )

    if not response.is_success:
        print(f"[AI] HTTP {response.status_code}. Body: {response.text[:1000]}", flush=True)
        response.raise_for_status()

    try:
        body = response.json()
        logger.info("Workspace Agent raw response for RFQ %s: %s", rfq_id_label, str(body)[:2000])
    except Exception as exc:
        logger.error(
            "Workspace Agent returned non-JSON body for RFQ %s: %s - body: %s",
            rfq_id_label,
            exc,
            response.text[:500],
        )
        raise ValueError("AI agent returned an invalid (non-JSON) response.") from exc

    result = _parse_agent_response(body)
    logger.info(
        "AI validation result - RFQ %s: approved=%s, fields_to_correct=%s",
        rfq_id_label,
        result.approved,
        result.fields_to_correct,
    )
    return result
