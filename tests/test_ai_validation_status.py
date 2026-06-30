import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User, UserRole
from app.routers.auth import create_access_token


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    role: UserRole,
    full_name: str | None = None,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=full_name or f"{prefix.title()} User",
        role=role,
        is_approved=True,
    )
    user.set_password("secure-password")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _headers_for(user: User) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(user.email, user.role.value)}"
    }


async def _create_rfq(
    db_session: AsyncSession,
    *,
    creator: User,
    validator: User | None = None,
) -> Rfq:
    systematic_rfq_id = f"26{uuid.uuid4().hex[:3].upper()}-BRU-00"
    rfq = Rfq(
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
        product_line_acronym="BRU",
        zone_manager_email=validator.email if validator else None,
        created_by_email=creator.email,
        rfq_data={
            "customer": "AI Validation Customer",
            "product_line_acronym": "BRU",
            "zone_manager_email": validator.email if validator else None,
            "systematic_rfq_id": systematic_rfq_id,
            "ai_validation": {
                "approved": True,
                "status": "queued",
                "message": "Workspace Agent trigger accepted and queued.",
                "discussion": "",
                "conversation_url": "",
                "fields_to_correct": [],
                "checked_at": "2026-06-30T09:00:00+00:00",
                "source": "workspace_agent_trigger",
            },
        },
        chat_history=[],
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)
    return rfq


@pytest.mark.asyncio
async def test_internal_ai_validation_callback_updates_rfq_by_systematic_id(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    monkeypatch.setenv("AI_VALIDATION_CALLBACK_TOKEN", "test-ai-callback-token")
    creator = await _create_user(
        db_session,
        prefix="ai-validation-callback-creator",
        role=UserRole.COMMERCIAL,
    )
    validator = await _create_user(
        db_session,
        prefix="ai-validation-callback-validator",
        role=UserRole.ZONE_MANAGER,
    )
    rfq = await _create_rfq(db_session, creator=creator, validator=validator)
    systematic_rfq_id = str((rfq.rfq_data or {}).get("systematic_rfq_id") or "")

    response = await client.post(
        "/api/internal/ai-validation",
        headers={"X-AI-Validation-Token": "test-ai-callback-token"},
        json={
            "systematic_rfq_id": systematic_rfq_id,
            "status": "rejected",
            "approved": False,
            "message": "Missing target price.",
            "discussion": "The RFQ cannot be approved until the target price is present.",
            "fields_to_correct": ["target_price"],
            "conversation_url": "https://chatgpt.com/c/test-conversation",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rfq_id"] == rfq.rfq_id
    assert payload["systematic_rfq_id"] == systematic_rfq_id
    assert payload["approved"] is False
    assert payload["status"] == "completed"
    assert payload["fields_to_correct"] == ["target_price"]
    assert payload["conversation_url"] == "https://chatgpt.com/c/test-conversation"

    await db_session.refresh(rfq)
    ai_validation = (rfq.rfq_data or {}).get("ai_validation") or {}
    assert ai_validation["approved"] is False
    assert ai_validation["status"] == "completed"
    assert ai_validation["message"] == "Missing target price."
    assert ai_validation["source"] == "workspace_agent_mcp"


@pytest.mark.asyncio
async def test_get_rfq_ai_validation_status_returns_saved_status(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="ai-validation-status-creator",
        role=UserRole.COMMERCIAL,
    )
    validator = await _create_user(
        db_session,
        prefix="ai-validation-status-validator",
        role=UserRole.ZONE_MANAGER,
    )
    rfq = await _create_rfq(db_session, creator=creator, validator=validator)

    response = await client.get(
        f"/api/rfq/{rfq.rfq_id}/ai-validation-status",
        headers=_headers_for(creator),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["approved"] is True
    assert payload["status"] == "queued"
    assert payload["message"] == "Workspace Agent trigger accepted and queued."
    assert payload["source"] == "workspace_agent_trigger"
