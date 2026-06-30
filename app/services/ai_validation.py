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

import asyncio
import base64
import datetime
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings

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


# ── PDF / file extraction for agent payload ───────────────────────────────────

_MAX_FILE_TEXT_CHARS = 8_000  # Truncate to keep agent token budget sane
_PDF_VISION_MAX_PAGES = 2     # Pages rendered for GPT-4 Vision fallback
_BLOB_DOWNLOAD_TIMEOUT = 30   # Seconds per file for Azure download


def _extract_text_from_pdf_bytes(content: bytes) -> str:
    """Return selectable text from a PDF binary using PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ""
    text_parts: list[str] = []
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            for page_num, page in enumerate(doc):
                if page_num >= 5:
                    break
                text = page.get_text()
                if text.strip():
                    text_parts.append(text.strip())
    except Exception as exc:
        logger.debug("PyMuPDF text extraction failed: %s", exc)
    return "\n\n".join(text_parts)


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
    azure_connection_string: str,
    azure_container: str,
    openai_api_key: str | None = None,
) -> list[dict]:
    """Enrich rfq_files with agent_file_text / agent_file_text_status before sending to agent.

    Status values the agent preface interprets:
      ok_text           — text layer extracted successfully by PyMuPDF
      ok_vision         — image PDF described by GPT-4 Vision
      vision_unavailable — image PDF, no OpenAI key configured
      vision_failed     — Vision API returned nothing useful
      download_failed   — blob unreachable (the only status that should block)
    """
    if not rfq_files or not azure_connection_string:
        return rfq_files

    enriched: list[dict] = []
    for file_entry in rfq_files:
        f = dict(file_entry)
        file_id = str(f.get("id") or "").strip()
        blob_name = str(f.get("blob_name") or "").strip()
        filename = str(f.get("filename") or f.get("name") or blob_name)
        content_type = str(f.get("content_type") or "application/octet-stream")

        # Always expose the proxy URL as the reference the agent sees.
        if file_id and not f.get("proxy_url"):
            f["proxy_url"] = f"{backend_base_url}/api/rfq/files/{file_id}/proxy"
        f["agent_file_url"] = f.get("proxy_url") or f.get("url") or ""

        if not blob_name:
            f["agent_file_text_status"] = "download_failed"
            enriched.append(f)
            continue

        try:
            content = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    _download_blob_sync,
                    azure_connection_string,
                    azure_container,
                    blob_name,
                ),
                timeout=_BLOB_DOWNLOAD_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Blob download failed for agent (%s): %s", blob_name, exc)
            f["agent_file_text_status"] = "download_failed"
            enriched.append(f)
            continue

        is_pdf = "pdf" in content_type.lower() or filename.lower().endswith(".pdf")

        if is_pdf:
            text = await asyncio.get_event_loop().run_in_executor(
                None, _extract_text_from_pdf_bytes, content
            )
            if text.strip():
                f["agent_file_text"] = text[:_MAX_FILE_TEXT_CHARS]
                f["agent_file_text_status"] = "ok_text"
            elif openai_api_key and len(content) < 20 * 1024 * 1024:
                vision_text = await _analyze_pdf_with_vision(content, openai_api_key, filename)
                if vision_text.strip():
                    f["agent_file_text"] = vision_text[:_MAX_FILE_TEXT_CHARS]
                    f["agent_file_text_status"] = "ok_vision"
                else:
                    f["agent_file_text_status"] = "vision_failed"
            else:
                f["agent_file_text_status"] = "vision_unavailable"
        else:
            try:
                f["agent_file_text"] = content.decode("utf-8", errors="replace")[:_MAX_FILE_TEXT_CHARS]
                f["agent_file_text_status"] = "ok_text"
            except Exception:
                f["agent_file_text_status"] = "vision_unavailable"

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

    payload = {"input": json.dumps(rfq_data, ensure_ascii=False, default=str)}
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
