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
    phase: RfqPhase,
    sub_status: RfqSubStatus,
    validator: User | None = None,
    product_line_acronym: str = "BRU",
) -> Rfq:
    rfq = Rfq(
        phase=phase,
        sub_status=sub_status,
        product_line_acronym=product_line_acronym,
        zone_manager_email=validator.email if validator else None,
        created_by_email=creator.email,
        rfq_data={
            "customer": "RBAC Customer",
            "product_line_acronym": product_line_acronym,
            "zone_manager_email": validator.email if validator else None,
            "systematic_rfq_id": f"26{uuid.uuid4().hex[:3].upper()}-{product_line_acronym}-00",
        },
        chat_history=[],
        costing_file_state={"file_status": "PENDING"} if phase == RfqPhase.COSTING else None,
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)
    return rfq


@pytest.mark.asyncio
async def test_costing_specialist_roles_cannot_mutate_rfq_phase(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="rbac-creator",
        role=UserRole.COMMERCIAL,
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.NEW_RFQ,
    )
    blocked_users = [
        await _create_user(db_session, prefix="rbac-costing", role=UserRole.COSTING_TEAM),
        await _create_user(db_session, prefix="rbac-rnd", role=UserRole.RND),
        await _create_user(db_session, prefix="rbac-plm", role=UserRole.PLM),
    ]

    for user in blocked_users:
        headers = _headers_for(user)
        data_response = await client.put(
            f"/api/rfq/{rfq.rfq_id}/data",
            json={"rfq_data": {"customer": "Blocked"}},
            headers=headers,
        )
        upload_response = await client.post(
            f"/api/rfq/{rfq.rfq_id}/upload",
            files={"file": ("drawing.pdf", b"blocked", "application/pdf")},
            headers=headers,
        )
        submit_response = await client.post(
            f"/api/rfq/{rfq.rfq_id}/submit",
            headers=headers,
        )
        chat_response = await client.post(
            "/api/chat",
            json={"rfq_id": rfq.rfq_id, "message": "try edit", "chat_mode": "rfq"},
            headers=headers,
        )

        assert data_response.status_code == 403
        assert upload_response.status_code == 403
        assert submit_response.status_code == 403
        assert chat_response.status_code == 403


@pytest.mark.asyncio
async def test_creator_and_assigned_validator_can_update_rfq_data(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="rbac-owner",
        role=UserRole.COMMERCIAL,
    )
    validator = await _create_user(
        db_session,
        prefix="rbac-validator",
        role=UserRole.ZONE_MANAGER,
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        validator=validator,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    creator_response = await client.put(
        f"/api/rfq/{rfq.rfq_id}/data",
        json={"rfq_data": {"customer": "Creator Edit"}},
        headers=_headers_for(creator),
    )
    validator_response = await client.put(
        f"/api/rfq/{rfq.rfq_id}/data",
        json={"rfq_data": {"project_name": "Validator Edit"}},
        headers=_headers_for(validator),
    )

    assert creator_response.status_code == 200
    assert validator_response.status_code == 200


@pytest.mark.asyncio
async def test_commercial_and_validator_cannot_post_costing_messages(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="rbac-costing-creator",
        role=UserRole.COMMERCIAL,
    )
    validator = await _create_user(
        db_session,
        prefix="rbac-costing-validator",
        role=UserRole.ZONE_MANAGER,
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        validator=validator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
    )

    for user in (creator, validator):
        response = await client.post(
            f"/api/rfq/{rfq.rfq_id}/costing-messages",
            json={
                "message": "This should be read-only.",
                "recipient_email": "team@avocarbon.com",
            },
            headers=_headers_for(user),
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_offer_advance_is_allowed_for_creator_and_denied_for_costing_role(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="rbac-offer-creator",
        role=UserRole.COMMERCIAL,
    )
    costing_user = await _create_user(
        db_session,
        prefix="rbac-offer-costing",
        role=UserRole.COSTING_TEAM,
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
    )

    denied_response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/advance",
        json={"target_phase": "OFFER", "target_sub_status": "VALIDATION"},
        headers=_headers_for(costing_user),
    )
    allowed_response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/advance",
        json={"target_phase": "OFFER", "target_sub_status": "VALIDATION"},
        headers=_headers_for(creator),
    )

    assert denied_response.status_code == 403
    assert allowed_response.status_code == 200
    assert allowed_response.json()["sub_status"] == "VALIDATION"
