"""Minimal MCP (Model Context Protocol) SSE server.

Exposes a single tool — save_ai_validation_result — so that a ChatGPT
Workspace Agent can call back directly via the MCP protocol instead of
the plain REST endpoint at /api/internal/ai-validation.

Configure in ChatGPT Workspace > Nouvelle application:
  URL  : https://<your-backend>/api/mcp/sse
  Auth : Aucune

MCP SSE transport (https://spec.modelcontextprotocol.io):
  1. Client GETs /api/mcp/sse  → server keeps the connection open and
     immediately sends:  event: endpoint\ndata: /api/mcp/messages?session=<id>
  2. Client POSTs JSON-RPC 2.0 messages to /api/mcp/messages?session=<id>
  3. Server returns HTTP 202 and pushes the JSON-RPC response onto the SSE stream.
"""

import asyncio
import json
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
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

# session_id → asyncio.Queue that feeds the SSE stream for that session
_sessions: dict[str, asyncio.Queue] = {}

# ── Tool definition (returned for tools/list) ────────────────────────────────

_TOOL = {
    "name": "save_ai_validation_result",
    "description": (
        "Save the AI triage decision for an RFQ to the Sales App backend database. "
        "Call this tool at the end of every RFQ analysis with the full discussion text. "
        "The backend will automatically detect the triage outcome (Bloqué / Libéré) "
        "from the discussion if `approved` is not supplied."
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
                "description": "True = RFQ can proceed, False = blocked. Omit to auto-detect from discussion.",
            },
            "message": {
                "type": "string",
                "description": "One-line summary shown in the Sales App UI.",
            },
            "fields_to_correct": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Field names that need correction (for rejected RFQs).",
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
    approved_raw = arguments.get("approved")  # may be bool, None, or absent

    if not systematic_rfq_id:
        return {"error": "systematic_rfq_id is required"}
    if not discussion:
        return {"error": "discussion is required"}

    # Coerce approved_raw to bool | None
    if approved_raw is not None:
        approved_raw = bool(approved_raw)

    # Infer approval from French discussion text when not explicit
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
        "rfq_id": rfq.rfq_id,
        "systematic_rfq_id": systematic_rfq_id,
        "approved": resolved_approved,
        "status": normalized_status,
        "message": f"AI validation saved: {'approved' if resolved_approved else 'blocked'}.",
    }


# ── JSON-RPC dispatcher ───────────────────────────────────────────────────────


async def _dispatch(message: dict) -> dict | None:
    method = message.get("method", "")
    msg_id = message.get("id")

    # Notifications have no id and need no response.
    if method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
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
            is_error = "error" in result
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": is_error,
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


# ── SSE endpoint ──────────────────────────────────────────────────────────────


@router.get("/sse")
async def sse_endpoint(request: Request) -> StreamingResponse:
    """SSE connection endpoint — keeps the MCP session alive."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = queue

    base = str(request.base_url).rstrip("/")
    messages_url = f"{base}/api/mcp/messages?session={session_id}"

    async def event_stream() -> AsyncGenerator[str, None]:
        # MCP spec: first event MUST be 'endpoint' with the absolute messages URL.
        yield f"event: endpoint\ndata: {messages_url}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Messages endpoint ─────────────────────────────────────────────────────────


@router.post("/messages")
async def messages_endpoint(request: Request) -> Response:
    """Receives JSON-RPC 2.0 messages from the MCP client.

    Returns the JSON-RPC response directly in the HTTP body (Streamable HTTP
    transport) so the endpoint works even when the SSE connection lands on a
    different worker process.  The response is also pushed onto the SSE queue
    when a matching session exists (classic SSE transport).
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    response = await _dispatch(body)

    # Also forward via SSE if the session is alive on this worker.
    session_id = request.query_params.get("session", "").strip()
    if session_id and response is not None:
        queue = _sessions.get(session_id)
        if queue is not None:
            await queue.put(response)

    if response is not None:
        return Response(
            content=json.dumps(response),
            status_code=200,
            media_type="application/json",
        )
    return Response(status_code=202)