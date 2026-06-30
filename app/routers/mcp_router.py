"""MCP server — Streamable HTTP transport (spec 2025-03-26).

ChatGPT Workspace > Nouvelle application:
  URL  : https://<your-backend>/api/mcp
  Auth : Aucune

Protocol flow (Streamable HTTP):
  1. Client POSTs JSON-RPC to POST /api/mcp
  2. Server responds directly with JSON-RPC in the HTTP 200 body
  3. No shared sessions, no SSE required — works with multiple workers
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select

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

# ── Tool definition ───────────────────────────────────────────────────────────

_TOOL = {
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
            )
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
            "result": {"tools": [_TOOL]},
        }

    if method == "tools/call":
        params = message.get("params") or {}
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}

        if tool_name != "save_ai_validation_result":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        try:
            result = await _run_save_tool(arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": "error" in result,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
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