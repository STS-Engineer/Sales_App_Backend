"""MCP server — Streamable HTTP transport (spec 2025-03-26).

ChatGPT Workspace > Nouvelle application:
  URL  : https://<your-backend>/api/mcp
  Auth : Aucune

Protocol flow (Streamable HTTP):
  1. Client POSTs JSON-RPC to POST /api/mcp
  2. Server responds directly with JSON-RPC in the HTTP 200 body
  3. No shared sessions, no SSE required — works with multiple workers
"""

import base64
import json
import os
from functools import lru_cache

import fitz
import httpx
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select

from app.config import settings
from app.database import async_session_maker
from app.models.rfq import Rfq
from app.services.ai_validation import (
    build_ai_validation_record,
    current_timestamp_iso,
    infer_approved_from_discussion,
    normalize_ai_validation_status,
    resolve_ai_validation_approved,
)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

RFQ_FILES_CONTAINER = settings.azure_rfq_files_container or "rfq-files"
PDF_TEXT_MAX_PAGES = 8
PDF_TEXT_MAX_CHARS = 20000
OPENAI_FILE_ANALYSIS_MODEL = "gpt-4o"
OPENAI_FILE_ANALYSIS_MAX_FILE_BYTES = 50 * 1024 * 1024
OPENAI_FILE_ANALYSIS_TIMEOUT_SECONDS = 120.0
DEFAULT_FILE_ANALYSIS_QUESTION = (
    "Read this RFQ attachment as the original file and provide a precise summary "
    "for costing triage. Extract all concrete technical details visible in the "
    "document, including part numbers, dimensions, tolerances, materials, surface "
    "treatments, drawing revision, title-block data, notes, and any missing or "
    "ambiguous points."
)

# ── Tool definition ───────────────────────────────────────────────────────────

_SAVE_TOOL = {
    "name": "save_ai_validation_result",
    "description": (
        "Save the AI triage decision for an RFQ to the Sales App backend database. "
        "Call this tool at the end of every RFQ analysis with the full discussion text. "
        "The backend automatically detects Bloqué / Libéré from the discussion text "
        "if `approved` is not supplied."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "systematic_rfq_id": {
                "type": "string",
                "description": "RFQ identifier, e.g. 26511-ASS-00.",
            },
            "discussion": {
                "type": "string",
                "description": "Full triage analysis text produced by the agent.",
            },
            "approved": {
                "type": "boolean",
                "description": "True = can proceed, False = blocked. Omit to auto-detect.",
            },
            "message": {
                "type": "string",
                "description": "One-line summary shown in the Sales App UI.",
            },
            "fields_to_correct": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Field names that need correction (blocked RFQs).",
            },
            "conversation_url": {
                "type": "string",
                "description": "Link to this ChatGPT conversation.",
            },
        },
        "required": ["systematic_rfq_id", "discussion"],
    },
}

_READ_ATTACHMENT_TOOL = {
    "name": "read_rfq_attachment_text",
    "description": (
        "Read one RFQ attachment from Sales App storage and return deterministic text "
        "extracted from the file. Prefer this tool over opening raw attachment URLs. "
        "Use it before deciding that a PDF plan is inaccessible. For readable PDFs it "
        "returns extracted text; for image-only or unreadable PDFs it returns an "
        "explicit status instead of guessing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "systematic_rfq_id": {
                "type": "string",
                "description": "RFQ identifier, e.g. 26511-ASS-00.",
            },
            "file_id": {
                "type": "string",
                "description": "Optional RFQ file identifier from rfq_files[].id.",
            },
            "filename_contains": {
                "type": "string",
                "description": "Optional case-insensitive filename fragment if file_id is unknown.",
            },
        },
        "required": ["systematic_rfq_id"],
    },
    "annotations": {"readOnlyHint": True},
}

_READ_BLOB_ATTACHMENT_TOOL = {
    "name": "read_rfq_blob_attachment_text",
    "description": (
        "Download one RFQ attachment directly from Azure Blob storage and return "
        "deterministic text extracted from the file. Prefer this tool when the "
        "agent cannot access raw blob URLs itself."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "systematic_rfq_id": {
                "type": "string",
                "description": "RFQ identifier, e.g. 26511-ASS-00.",
            },
            "file_id": {
                "type": "string",
                "description": "Optional RFQ file identifier from rfq_files[].id.",
            },
            "filename_contains": {
                "type": "string",
                "description": "Optional case-insensitive filename fragment if file_id is unknown.",
            },
        },
        "required": ["systematic_rfq_id"],
    },
    "annotations": {"readOnlyHint": True},
}

_ANALYZE_BLOB_WITH_OPENAI_TOOL = {
    "name": "analyze_rfq_blob_attachment_with_openai",
    "description": (
        "Download one RFQ attachment directly from Azure Blob storage and analyze it "
        "as a true OpenAI input_file. Use this when the Workspace Agent must reason "
        "over the original PDF or document file instead of locally extracted text."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "systematic_rfq_id": {
                "type": "string",
                "description": "RFQ identifier, e.g. 26511-ASS-00.",
            },
            "file_id": {
                "type": "string",
                "description": "Optional RFQ file identifier from rfq_files[].id.",
            },
            "filename_contains": {
                "type": "string",
                "description": "Optional case-insensitive filename fragment if file_id is unknown.",
            },
            "question": {
                "type": "string",
                "description": (
                    "Optional analysis question for the file. If omitted, a generic "
                    "costing-triage summary prompt is used."
                ),
            },
        },
        "required": ["systematic_rfq_id"],
    },
    "annotations": {"readOnlyHint": True},
}

_TOOLS = [
    _SAVE_TOOL,
    _READ_ATTACHMENT_TOOL,
    _READ_BLOB_ATTACHMENT_TOOL,
    _ANALYZE_BLOB_WITH_OPENAI_TOOL,
]


@lru_cache(maxsize=1)
def _get_blob_service_client() -> BlobServiceClient:
    if not settings.azure_connection_string:
        raise RuntimeError("AZURE_CONNECTION_STRING is not configured.")
    return BlobServiceClient.from_connection_string(settings.azure_connection_string)


def _get_rfq_files_container_client():
    return _get_blob_service_client().get_container_client(RFQ_FILES_CONTAINER)


def _extract_pdf_text(content: bytes) -> str:
    if not content:
        return ""
    try:
        with fitz.open(stream=content, filetype="pdf") as pdf_document:
            chunks: list[str] = []
            max_pages = min(pdf_document.page_count, PDF_TEXT_MAX_PAGES)
            for page_index in range(max_pages):
                page = pdf_document.load_page(page_index)
                page_text = (page.get_text("text") or "").strip()
                if not page_text:
                    continue
                chunks.append(f"[Page {page_index + 1}]\n{page_text}")
                joined = "\n\n".join(chunks)
                if len(joined) >= PDF_TEXT_MAX_CHARS:
                    return joined[:PDF_TEXT_MAX_CHARS].rstrip() + "\n\n[Truncated]"
            return "\n\n".join(chunks).strip()
    except Exception:
        return ""


def _extract_attachment_text(content: bytes, content_type: str, filename: str) -> str:
    normalized_type = str(content_type or "").strip().lower()
    extension = os.path.splitext(str(filename or "").strip().lower())[1]

    if normalized_type == "application/pdf" or extension == ".pdf":
        return _extract_pdf_text(content)

    if normalized_type.startswith("text/") or extension in {".txt", ".csv", ".json", ".xml"}:
        try:
            return content.decode("utf-8", errors="ignore").strip()[:PDF_TEXT_MAX_CHARS]
        except Exception:
            return ""

    return ""


def _pick_rfq_file_entry(
    rfq_files: list[dict],
    *,
    file_id: str = "",
    filename_contains: str = "",
) -> dict | None:
    normalized_file_id = file_id.strip()
    if normalized_file_id:
        return next(
            (
                entry
                for entry in rfq_files
                if str(entry.get("id") or entry.get("file_id") or "").strip()
                == normalized_file_id
            ),
            None,
        )

    normalized_filename = filename_contains.strip().lower()
    if normalized_filename:
        return next(
            (
                entry
                for entry in rfq_files
                if normalized_filename in str(
                    entry.get("filename") or entry.get("name") or ""
                )
                .strip()
                .lower()
            ),
            None,
        )

    return rfq_files[0] if rfq_files else None


async def _download_attachment_bytes(file_entry: dict) -> tuple[bytes | None, str]:
    candidate_urls = [
        str(file_entry.get("download_url") or "").strip(),
        str(file_entry.get("url") or "").strip(),
        str(file_entry.get("path") or "").strip(),
        str(file_entry.get("proxy_url") or "").strip(),
    ]
    candidate_urls = [url for url in candidate_urls if url]

    for candidate_url in candidate_urls:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
                follow_redirects=True,
                trust_env=False,
            ) as client:
                response = await client.get(candidate_url)
            if response.status_code == 200 and response.content:
                return response.content, str(
                    response.headers.get("content-type")
                    or file_entry.get("content_type")
                    or "application/octet-stream"
                ).strip()
        except Exception:
            continue

    blob_name = str(file_entry.get("blob_name") or "").strip()
    if not blob_name or not settings.azure_connection_string:
        return None, ""

    try:
        blob_client = _get_rfq_files_container_client().get_blob_client(blob_name)
        content = blob_client.download_blob().readall()
        return content, str(file_entry.get("content_type") or "application/octet-stream").strip()
    except ResourceNotFoundError:
        return None, ""
    except Exception:
        return None, ""


async def _download_blob_attachment_bytes(file_entry: dict) -> tuple[bytes | None, str, str]:
    candidate_urls = [
        str(file_entry.get("download_url") or "").strip(),
        str(file_entry.get("url") or "").strip(),
        str(file_entry.get("path") or "").strip(),
    ]
    candidate_urls = [url for url in candidate_urls if url]

    for candidate_url in candidate_urls:
        if "blob.core.windows.net" not in candidate_url.lower():
            continue
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
                follow_redirects=True,
                trust_env=False,
            ) as client:
                response = await client.get(candidate_url)
            if response.status_code == 200 and response.content:
                return (
                    response.content,
                    str(
                        response.headers.get("content-type")
                        or file_entry.get("content_type")
                        or "application/octet-stream"
                    ).strip(),
                    "azure_blob_sas_url",
                )
        except Exception:
            continue

    blob_name = str(file_entry.get("blob_name") or "").strip()
    if not blob_name or not settings.azure_connection_string:
        return None, "", ""

    try:
        blob_client = _get_rfq_files_container_client().get_blob_client(blob_name)
        content = blob_client.download_blob().readall()
        return (
            content,
            str(file_entry.get("content_type") or "application/octet-stream").strip(),
            "azure_blob_sdk",
        )
    except ResourceNotFoundError:
        return None, "", ""
    except Exception:
        return None, "", ""


def _resolve_attachment_content_type(content_type: str, filename: str) -> str:
    normalized_type = str(content_type or "").strip().lower()
    if normalized_type:
        return normalized_type

    extension = os.path.splitext(str(filename or "").strip().lower())[1]
    if extension == ".pdf":
        return "application/pdf"
    if extension == ".txt":
        return "text/plain"
    if extension == ".csv":
        return "text/csv"
    if extension == ".json":
        return "application/json"
    return "application/octet-stream"


def _extract_responses_output_text(body: dict) -> str:
    direct_text = str(body.get("output_text") or "").strip()
    if direct_text:
        return direct_text

    chunks: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for content_part in item.get("content") or []:
            if not isinstance(content_part, dict):
                continue
            if content_part.get("type") == "output_text":
                text = str(content_part.get("text") or "").strip()
                if text:
                    chunks.append(text)
    return "\n\n".join(chunks).strip()


async def _analyze_file_with_openai(
    *,
    content: bytes,
    content_type: str,
    filename: str,
    question: str,
) -> dict:
    openai_api_key = str(settings.OPENAI_API_KEY or "").strip()
    if not openai_api_key:
        return {"error": "OPENAI_API_KEY is not configured."}

    if not content:
        return {"error": "Attachment content is empty."}

    if len(content) > OPENAI_FILE_ANALYSIS_MAX_FILE_BYTES:
        return {
            "error": (
                "Attachment exceeds the OpenAI input_file size limit of 50 MB."
            )
        }

    normalized_type = _resolve_attachment_content_type(content_type, filename)
    file_data = (
        f"data:{normalized_type};base64,"
        f"{base64.b64encode(content).decode('ascii')}"
    )
    request_body = {
        "model": OPENAI_FILE_ANALYSIS_MODEL,
        "store": False,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": question},
                    {
                        "type": "input_file",
                        "filename": filename or "attachment",
                        "file_data": file_data,
                    },
                ],
            }
        ],
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(OPENAI_FILE_ANALYSIS_TIMEOUT_SECONDS)
    ) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )

    if not response.is_success:
        return {
            "error": "OpenAI file analysis request failed.",
            "status_code": response.status_code,
            "body": response.text[:1000],
        }

    try:
        body = response.json()
    except Exception:
        return {
            "error": "OpenAI file analysis returned non-JSON data.",
            "status_code": response.status_code,
            "body": response.text[:1000],
        }

    output_text = _extract_responses_output_text(body)
    if not output_text:
        return {
            "error": "OpenAI file analysis returned no readable output.",
            "response_id": str(body.get("id") or "").strip(),
            "status": str(body.get("status") or "").strip(),
        }

    return {
        "success": True,
        "response_id": str(body.get("id") or "").strip(),
        "status": str(body.get("status") or "").strip() or "completed",
        "model": str(body.get("model") or OPENAI_FILE_ANALYSIS_MODEL),
        "question": question,
        "analysis": output_text,
    }

# ── Tool executor ─────────────────────────────────────────────────────────────


async def _run_save_tool(arguments: dict) -> dict:
    systematic_rfq_id = str(arguments.get("systematic_rfq_id") or "").strip()
    discussion = str(arguments.get("discussion") or "").strip()
    conversation_url = str(arguments.get("conversation_url") or "").strip()
    message = str(arguments.get("message") or "").strip()
    fields_to_correct = [str(f) for f in (arguments.get("fields_to_correct") or []) if f]
    approved_raw = arguments.get("approved")

    if not systematic_rfq_id:
        return {"error": "systematic_rfq_id is required"}
    if not discussion:
        return {"error": "discussion is required"}

    if approved_raw is not None:
        approved_raw = bool(approved_raw)

    effective_approved = approved_raw
    if effective_approved is None:
        effective_approved = infer_approved_from_discussion(discussion)

    resolved_approved = resolve_ai_validation_approved(
        effective_approved, None, fallback=True
    )
    normalized_status = normalize_ai_validation_status("completed", resolved_approved)

    async with async_session_maker() as db:
        result = await db.execute(
            select(Rfq).where(
                Rfq.rfq_data["systematic_rfq_id"].astext == systematic_rfq_id
            ).limit(1)
        )
        rfq = result.scalar_one_or_none()
        if rfq is None:
            return {"error": f"RFQ not found: {systematic_rfq_id}"}

        rfq_id = rfq.rfq_id
        rfq_data = dict(rfq.rfq_data or {})
        rfq_data["ai_validation"] = build_ai_validation_record(
            approved=resolved_approved,
            status=normalized_status,
            message=message,
            discussion=discussion,
            fields_to_correct=fields_to_correct,
            conversation_url=conversation_url,
            checked_at=current_timestamp_iso(),
            source="workspace_agent_mcp",
        )
        rfq.rfq_data = rfq_data
        await db.commit()

    return {
        "success": True,
        "rfq_id": rfq_id,
        "systematic_rfq_id": systematic_rfq_id,
        "approved": resolved_approved,
        "status": normalized_status,
        "message": f"AI validation saved: {'approved' if resolved_approved else 'blocked'}.",
    }


async def _run_read_attachment_tool(arguments: dict) -> dict:
    systematic_rfq_id = str(arguments.get("systematic_rfq_id") or "").strip()
    file_id = str(arguments.get("file_id") or "").strip()
    filename_contains = str(arguments.get("filename_contains") or "").strip()

    if not systematic_rfq_id:
        return {"error": "systematic_rfq_id is required"}

    async with async_session_maker() as db:
        result = await db.execute(
            select(Rfq).where(
                Rfq.rfq_data["systematic_rfq_id"].astext == systematic_rfq_id
            ).limit(1)
        )
        rfq = result.scalar_one_or_none()
        if rfq is None:
            return {"error": f"RFQ not found: {systematic_rfq_id}"}

        rfq_files = [
            entry
            for entry in list((rfq.rfq_data or {}).get("rfq_files") or [])
            if isinstance(entry, dict)
        ]
        if not rfq_files:
            return {
                "error": "No RFQ attachments are registered for this RFQ.",
                "systematic_rfq_id": systematic_rfq_id,
            }

        file_entry = _pick_rfq_file_entry(
            rfq_files,
            file_id=file_id,
            filename_contains=filename_contains,
        )
        if file_entry is None:
            return {
                "error": "Requested RFQ attachment was not found.",
                "systematic_rfq_id": systematic_rfq_id,
            }

    filename = str(
        file_entry.get("filename")
        or file_entry.get("name")
        or os.path.basename(str(file_entry.get("blob_name") or "").strip())
    ).strip()
    content, content_type = await _download_attachment_bytes(file_entry)
    if not content:
        return {
            "error": "Unable to download RFQ attachment from the registered sources.",
            "systematic_rfq_id": systematic_rfq_id,
            "file_id": str(file_entry.get("id") or "").strip(),
            "filename": filename,
        }

    extracted_text = _extract_attachment_text(content, content_type, filename)
    status = "ok" if extracted_text else "no_extractable_text"
    message = (
        "Attachment text extracted successfully."
        if extracted_text
        else "The attachment downloaded successfully but no readable text layer was extracted."
    )

    return {
        "success": True,
        "systematic_rfq_id": systematic_rfq_id,
        "file_id": str(file_entry.get("id") or "").strip(),
        "filename": filename,
        "content_type": content_type,
        "status": status,
        "message": message,
        "text_length": len(extracted_text),
        "text": extracted_text,
    }


async def _run_read_blob_attachment_tool(arguments: dict) -> dict:
    systematic_rfq_id = str(arguments.get("systematic_rfq_id") or "").strip()
    file_id = str(arguments.get("file_id") or "").strip()
    filename_contains = str(arguments.get("filename_contains") or "").strip()

    if not systematic_rfq_id:
        return {"error": "systematic_rfq_id is required"}

    async with async_session_maker() as db:
        result = await db.execute(
            select(Rfq).where(
                Rfq.rfq_data["systematic_rfq_id"].astext == systematic_rfq_id
            ).limit(1)
        )
        rfq = result.scalar_one_or_none()
        if rfq is None:
            return {"error": f"RFQ not found: {systematic_rfq_id}"}

        rfq_files = [
            entry
            for entry in list((rfq.rfq_data or {}).get("rfq_files") or [])
            if isinstance(entry, dict)
        ]
        if not rfq_files:
            return {
                "error": "No RFQ attachments are registered for this RFQ.",
                "systematic_rfq_id": systematic_rfq_id,
            }

        file_entry = _pick_rfq_file_entry(
            rfq_files,
            file_id=file_id,
            filename_contains=filename_contains,
        )
        if file_entry is None:
            return {
                "error": "Requested RFQ attachment was not found.",
                "systematic_rfq_id": systematic_rfq_id,
            }

    filename = str(
        file_entry.get("filename")
        or file_entry.get("name")
        or os.path.basename(str(file_entry.get("blob_name") or "").strip())
    ).strip()
    content, content_type, source = await _download_blob_attachment_bytes(file_entry)
    if not content:
        return {
            "error": "Unable to download RFQ attachment directly from Azure Blob storage.",
            "systematic_rfq_id": systematic_rfq_id,
            "file_id": str(file_entry.get("id") or "").strip(),
            "filename": filename,
            "blob_name": str(file_entry.get("blob_name") or "").strip(),
        }

    extracted_text = _extract_attachment_text(content, content_type, filename)
    status = "ok" if extracted_text else "no_extractable_text"
    message = (
        "Blob attachment text extracted successfully."
        if extracted_text
        else "The blob attachment downloaded successfully but no readable text layer was extracted."
    )

    return {
        "success": True,
        "systematic_rfq_id": systematic_rfq_id,
        "file_id": str(file_entry.get("id") or "").strip(),
        "filename": filename,
        "blob_name": str(file_entry.get("blob_name") or "").strip(),
        "content_type": content_type,
        "source": source,
        "status": status,
        "message": message,
        "text_length": len(extracted_text),
        "text": extracted_text,
    }


async def _run_analyze_blob_with_openai_tool(arguments: dict) -> dict:
    systematic_rfq_id = str(arguments.get("systematic_rfq_id") or "").strip()
    file_id = str(arguments.get("file_id") or "").strip()
    filename_contains = str(arguments.get("filename_contains") or "").strip()
    question = str(arguments.get("question") or "").strip() or DEFAULT_FILE_ANALYSIS_QUESTION

    if not systematic_rfq_id:
        return {"error": "systematic_rfq_id is required"}

    async with async_session_maker() as db:
        result = await db.execute(
            select(Rfq).where(
                Rfq.rfq_data["systematic_rfq_id"].astext == systematic_rfq_id
            ).limit(1)
        )
        rfq = result.scalar_one_or_none()
        if rfq is None:
            return {"error": f"RFQ not found: {systematic_rfq_id}"}

        rfq_files = [
            entry
            for entry in list((rfq.rfq_data or {}).get("rfq_files") or [])
            if isinstance(entry, dict)
        ]
        if not rfq_files:
            return {
                "error": "No RFQ attachments are registered for this RFQ.",
                "systematic_rfq_id": systematic_rfq_id,
            }

        file_entry = _pick_rfq_file_entry(
            rfq_files,
            file_id=file_id,
            filename_contains=filename_contains,
        )
        if file_entry is None:
            return {
                "error": "Requested RFQ attachment was not found.",
                "systematic_rfq_id": systematic_rfq_id,
            }

    filename = str(
        file_entry.get("filename")
        or file_entry.get("name")
        or os.path.basename(str(file_entry.get("blob_name") or "").strip())
    ).strip()
    content, content_type, source = await _download_blob_attachment_bytes(file_entry)
    if not content:
        return {
            "error": "Unable to download RFQ attachment directly from Azure Blob storage.",
            "systematic_rfq_id": systematic_rfq_id,
            "file_id": str(file_entry.get("id") or "").strip(),
            "filename": filename,
            "blob_name": str(file_entry.get("blob_name") or "").strip(),
        }

    analysis_result = await _analyze_file_with_openai(
        content=content,
        content_type=content_type,
        filename=filename,
        question=question,
    )
    if "error" in analysis_result:
        return {
            **analysis_result,
            "systematic_rfq_id": systematic_rfq_id,
            "file_id": str(file_entry.get("id") or "").strip(),
            "filename": filename,
            "blob_name": str(file_entry.get("blob_name") or "").strip(),
            "content_type": _resolve_attachment_content_type(content_type, filename),
            "source": source,
        }

    return {
        "success": True,
        "systematic_rfq_id": systematic_rfq_id,
        "file_id": str(file_entry.get("id") or "").strip(),
        "filename": filename,
        "blob_name": str(file_entry.get("blob_name") or "").strip(),
        "content_type": _resolve_attachment_content_type(content_type, filename),
        "source": source,
        **analysis_result,
    }


# ── JSON-RPC dispatcher ───────────────────────────────────────────────────────


async def _dispatch(message: dict) -> dict | None:
    method = message.get("method", "")
    msg_id = message.get("id")

    if method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rfq-ai-validation", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": _TOOLS},
        }

    if method == "tools/call":
        params = message.get("params") or {}
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}

        try:
            if tool_name == "save_ai_validation_result":
                result = await _run_save_tool(arguments)
            elif tool_name == "read_rfq_attachment_text":
                result = await _run_read_attachment_tool(arguments)
            elif tool_name == "read_rfq_blob_attachment_text":
                result = await _run_read_blob_attachment_tool(arguments)
            elif tool_name == "analyze_rfq_blob_attachment_with_openai":
                result = await _run_analyze_blob_with_openai_tool(arguments)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "structuredContent": result,
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": "error" in result,
                },
            }
        except Exception as exc:
            error_result = {"error": str(exc)}
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "structuredContent": error_result,
                    "content": [{"type": "text", "text": json.dumps(error_result)}],
                    "isError": True,
                },
            }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


# ── Streamable HTTP endpoint (MCP 2025-03-26) ─────────────────────────────────


@router.post("")
async def mcp_post(request: Request) -> Response:
    """Main MCP endpoint — Streamable HTTP transport.

    ChatGPT POSTs JSON-RPC here and receives the response directly in the
    HTTP body.  No shared in-memory sessions needed.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    response = await _dispatch(body)

    if response is not None:
        return Response(
            content=json.dumps(response),
            status_code=200,
            media_type="application/json",
            headers={"MCP-Protocol-Version": "2025-03-26"},
        )
    return Response(status_code=202)


@router.get("")
async def mcp_get() -> Response:
    """Health-check / capability probe for the MCP endpoint."""
    return Response(
        content=json.dumps({
            "server": "rfq-ai-validation",
            "protocolVersion": "2025-03-26",
            "transport": "streamable-http",
        }),
        status_code=200,
        media_type="application/json",
    )
