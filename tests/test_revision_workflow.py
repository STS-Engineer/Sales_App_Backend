import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User, UserRole
from app.routers import rfq as rfq_router
from app.routers.auth import create_access_token


async def _create_user_and_headers(
    db_session: AsyncSession,
    *,
    role: UserRole,
    email_prefix: str,
) -> tuple[User, dict[str, str]]:
    email = f"{email_prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com"
    user = User(
        email=email,
        full_name=email_prefix.replace("-", " ").title(),
        role=role,
        is_approved=True,
    )
    user.set_password("secure-password")
    db_session.add(user)
    await db_session.commit()
    return user, {
        "Authorization": f"Bearer {create_access_token(user.email, user.role.value)}"
    }


async def _create_rfq(
    db_session: AsyncSession,
    *,
    creator_email: str,
    validator_email: str,
    sub_status: RfqSubStatus,
    revision_notes: str | None = None,
) -> Rfq:
    rfq = Rfq(
        phase=RfqPhase.RFQ,
        sub_status=sub_status,
        created_by_email=creator_email,
        zone_manager_email=validator_email,
        rfq_data={
            "zone_manager_email": validator_email,
            "systematic_rfq_id": f"26{uuid.uuid4().hex[:3].upper()}-BRU-00",
        },
        chat_history=[],
        revision_notes=revision_notes,
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)
    return rfq


@pytest.mark.asyncio
async def test_assigned_validator_can_request_revision_and_notes_are_serialized(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator, creator_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator",
    )
    validator, validator_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator",
    )
    rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
    )

    email_calls: list[tuple[str, str, str]] = []

    def _fake_send_revision_request_email(
        sales_rep_email: str,
        systematic_rfq_id: str,
        comment: str,
        _rfq_link: str,
    ) -> bool:
        email_calls.append((sales_rep_email, systematic_rfq_id, comment))
        return True

    monkeypatch.setattr(
        rfq_router.emails,
        "send_revision_request_email",
        _fake_send_revision_request_email,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/request-revision",
        json={"comment": "Please update the quotation date."},
        headers=validator_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "RFQ"
    assert payload["sub_status"] == "REVISION_REQUESTED"
    assert payload["revision_notes"] == "Please update the quotation date."
    assert email_calls == [
        (
            creator.email,
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            "Please update the quotation date.",
        )
    ]

    detail_response = await client.get(
        f"/api/rfq/{rfq.rfq_id}",
        headers=creator_headers,
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["revision_notes"] == "Please update the quotation date."

    list_response = await client.get("/api/rfq", headers=creator_headers)
    assert list_response.status_code == 200
    listed = next(item for item in list_response.json() if item["rfq_id"] == rfq.rfq_id)
    assert listed["revision_notes"] == "Please update the quotation date."


@pytest.mark.asyncio
async def test_owner_can_request_revision(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator",
    )
    validator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator",
    )
    owner, owner_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.OWNER,
        email_prefix="owner",
    )
    rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
    )

    monkeypatch.setattr(
        rfq_router.emails,
        "send_revision_request_email",
        lambda *args, **kwargs: True,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/request-revision",
        json={"comment": "Please review the contact details."},
        headers=owner_headers,
    )

    assert response.status_code == 200
    assert response.json()["sub_status"] == "REVISION_REQUESTED"


@pytest.mark.asyncio
async def test_request_revision_rejects_non_validator_wrong_state_and_blank_comment(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator, creator_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator",
    )
    validator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator",
    )
    other_user, other_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="other",
    )

    pending_rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
    )
    wrong_state_rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    forbidden_response = await client.post(
        f"/api/rfq/{pending_rfq.rfq_id}/request-revision",
        json={"comment": "Please revise this RFQ."},
        headers=creator_headers,
    )
    assert forbidden_response.status_code == 403

    other_forbidden = await client.post(
        f"/api/rfq/{pending_rfq.rfq_id}/request-revision",
        json={"comment": "Please revise this RFQ."},
        headers=other_headers,
    )
    assert other_forbidden.status_code == 403

    blank_response = await client.post(
        f"/api/rfq/{pending_rfq.rfq_id}/request-revision",
        json={"comment": "   "},
        headers={"Authorization": f"Bearer {create_access_token(validator.email, validator.role.value)}"},
    )
    assert blank_response.status_code == 400

    wrong_state_response = await client.post(
        f"/api/rfq/{wrong_state_rfq.rfq_id}/request-revision",
        json={"comment": "Please revise this RFQ."},
        headers={"Authorization": f"Bearer {create_access_token(validator.email, validator.role.value)}"},
    )
    assert wrong_state_response.status_code == 400


@pytest.mark.asyncio
async def test_creator_can_submit_revision_and_clear_notes(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator, creator_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator",
    )
    validator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator",
    )
    rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.REVISION_REQUESTED,
        revision_notes="Please update the quotation date.",
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/submit-revision",
        headers=creator_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "RFQ"
    assert payload["sub_status"] == "PENDING_FOR_VALIDATION"
    assert payload["revision_notes"] is None


@pytest.mark.asyncio
async def test_owner_can_submit_revision(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator",
    )
    validator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator",
    )
    owner, owner_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.OWNER,
        email_prefix="owner",
    )
    rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.REVISION_REQUESTED,
        revision_notes="Please update the pricing assumptions.",
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/submit-revision",
        headers=owner_headers,
    )

    assert response.status_code == 200
    assert response.json()["sub_status"] == "PENDING_FOR_VALIDATION"


@pytest.mark.asyncio
async def test_submit_revision_rejects_non_creator_and_wrong_state(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator",
    )
    validator, validator_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator",
    )
    rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.REVISION_REQUESTED,
        revision_notes="Please revise the RFQ notes.",
    )
    wrong_state_rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
    )

    forbidden_response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/submit-revision",
        headers=validator_headers,
    )
    assert forbidden_response.status_code == 403

    wrong_state_response = await client.post(
        f"/api/rfq/{wrong_state_rfq.rfq_id}/submit-revision",
        headers={"Authorization": f"Bearer {create_access_token(creator.email, creator.role.value)}"},
    )
    assert wrong_state_response.status_code == 400


@pytest.mark.asyncio
async def test_update_rfq_data_normalizes_target_price_is_estimated_boolean(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator, creator_headers = await _create_user_and_headers(
        db_session,
        role=UserRole.COMMERCIAL,
        email_prefix="creator-bool",
    )
    validator, _ = await _create_user_and_headers(
        db_session,
        role=UserRole.ZONE_MANAGER,
        email_prefix="validator-bool",
    )
    rfq = await _create_rfq(
        db_session,
        creator_email=creator.email,
        validator_email=validator.email,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    true_response = await client.put(
        f"/api/rfq/{rfq.rfq_id}/data",
        json={"rfq_data": {"target_price_is_estimated": "yes"}},
        headers=creator_headers,
    )

    assert true_response.status_code == 200
    assert true_response.json()["rfq_data"]["target_price_is_estimated"] is True

    false_response = await client.put(
        f"/api/rfq/{rfq.rfq_id}/data",
        json={"rfq_data": {"target_price_is_estimated": "false"}},
        headers=creator_headers,
    )

    assert false_response.status_code == 200
    assert false_response.json()["rfq_data"]["target_price_is_estimated"] is False
