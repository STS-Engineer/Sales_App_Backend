from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.database import get_db
from app.models.rfq import Rfq
from app.schemas.rfq import AiValidationCallbackRequest, AiValidationCallbackResponse
from app.services.ai_validation import (
    build_ai_validation_record,
    current_timestamp_iso,
    extract_ai_validation_record,
    infer_approved_from_discussion,
    normalize_ai_validation_status,
    resolve_ai_validation_approved,
)
from app.tasks.followups import run_followup_sweep

router = APIRouter(prefix="/api/internal", tags=["internal"])


def _extract_bearer_token(authorization: str | None) -> str:
    value = str(authorization or "").strip()
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


async def _get_rfq_for_ai_validation_callback(
    db: AsyncSession,
    *,
    rfq_id: str | None,
    systematic_rfq_id: str | None,
) -> Rfq | None:
    normalized_rfq_id = str(rfq_id or "").strip()
    if normalized_rfq_id:
        return await db.get(Rfq, normalized_rfq_id)

    normalized_systematic_id = str(systematic_rfq_id or "").strip()
    if not normalized_systematic_id:
        return None

    result = await db.execute(
        select(Rfq).where(
            Rfq.rfq_data["systematic_rfq_id"].astext == normalized_systematic_id
        ).limit(1)
    )
    return result.scalar_one_or_none()


@router.post("/trigger-followups")
async def trigger_followups(
    x_cron_token: str | None = Header(default=None, alias="X-Cron-Token"),
    db: AsyncSession = Depends(get_db),
):
    configured_token = str(settings.CRON_TOKEN or "").strip()
    if not configured_token:
        raise HTTPException(status_code=503, detail="CRON_TOKEN is not configured.")
    if str(x_cron_token or "").strip() != configured_token:
        raise HTTPException(status_code=403, detail="Invalid cron token.")
    return await run_followup_sweep(db)


@router.post("/ai-validation", response_model=AiValidationCallbackResponse)
async def save_ai_validation_result(
    body: AiValidationCallbackRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_ai_validation_token: str | None = Header(
        default=None, alias="X-AI-Validation-Token"
    ),
    db: AsyncSession = Depends(get_db),
):
    runtime_settings = Settings()
    configured_token = runtime_settings.ai_validation_callback_token
    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="AI_VALIDATION_CALLBACK_TOKEN is not configured.",
        )

    provided_token = (
        str(x_ai_validation_token or "").strip()
        or _extract_bearer_token(authorization)
    )
    if provided_token != configured_token:
        raise HTTPException(status_code=401, detail="Invalid AI validation token.")

    rfq = await _get_rfq_for_ai_validation_callback(
        db,
        rfq_id=body.rfq_id,
        systematic_rfq_id=body.systematic_rfq_id,
    )
    if rfq is None:
        raise HTTPException(status_code=404, detail="RFQ not found.")

    existing_record = extract_ai_validation_record(rfq.rfq_data) or {}

    # If the agent did not send an explicit approved flag, infer it from the
    # discussion text (detects "Statut : Bloqué" and similar French phrases).
    effective_approved = body.approved
    if effective_approved is None:
        effective_approved = infer_approved_from_discussion(
            str(body.discussion or "")
        )

    resolved_approved = resolve_ai_validation_approved(
        effective_approved,
        body.status,
        fallback=bool(existing_record.get("approved", True)),
    )
    # Force status to "completed" when the discussion was successfully parsed.
    effective_status = body.status
    if effective_approved is not None and not body.status:
        effective_status = "completed"
    normalized_status = normalize_ai_validation_status(effective_status, effective_approved)
    rfq_data = dict(rfq.rfq_data or {})
    systematic_rfq_id = str(rfq_data.get("systematic_rfq_id") or "").strip() or None
    rfq_data["ai_validation"] = build_ai_validation_record(
        approved=resolved_approved,
        status=normalized_status,
        message=(
            str(body.message or "").strip()
            or str(existing_record.get("message") or "")
        ),
        discussion=(
            str(body.discussion or "").strip()
            or str(existing_record.get("discussion") or "")
        ),
        fields_to_correct=body.fields_to_correct,
        conversation_url=(
            str(body.conversation_url or "").strip()
            or str(existing_record.get("conversation_url") or "")
        ),
        checked_at=current_timestamp_iso(),
        source=str(body.source or "").strip() or "workspace_agent_mcp",
    )
    rfq.rfq_data = rfq_data
    await db.commit()
    await db.refresh(rfq)

    ai_validation = extract_ai_validation_record(rfq.rfq_data)
    if ai_validation is None:
        raise HTTPException(status_code=500, detail="AI validation status was not saved.")

    return AiValidationCallbackResponse(
        rfq_id=rfq.rfq_id,
        systematic_rfq_id=systematic_rfq_id,
        **ai_validation,
    )
