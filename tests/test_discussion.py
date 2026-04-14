import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discussion import DiscussionMessage
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User, UserRole
from app.routers.auth import create_access_token


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    role: UserRole,
    full_name: str,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=full_name,
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
    zone_manager: User | None = None,
    sub_status: RfqSubStatus = RfqSubStatus.NEW_RFQ,
) -> Rfq:
    rfq = Rfq(
        phase=RfqPhase.RFQ,
        sub_status=sub_status,
        zone_manager_email=zone_manager.email if zone_manager else None,
        created_by_email=creator.email,
        rfq_data={},
        chat_history=[],
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)
    return rfq


@pytest.mark.asyncio
async def test_discussion_post_stores_author_and_allows_assigned_viewer(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="discussion-creator",
        role=UserRole.COMMERCIAL,
        full_name="RFQ Creator",
    )
    zone_manager = await _create_user(
        db_session,
        prefix="discussion-zone",
        role=UserRole.ZONE_MANAGER,
        full_name="Zone Reviewer",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        zone_manager=zone_manager,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/discussion",
        json={"phase": "NEW_RFQ", "message": "  Need pricing support.  "},
        headers=_headers_for(zone_manager),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["rfq_id"] == rfq.rfq_id
    assert payload["phase"] == "NEW_RFQ"
    assert payload["message"] == "Need pricing support."
    assert payload["user_id"] == zone_manager.user_id
    assert payload["author_name"] == "Zone Reviewer"
    assert payload["author_email"] == zone_manager.email
    assert payload["author_role"] == "ZONE_MANAGER"

    result = await db_session.execute(
        select(DiscussionMessage).where(DiscussionMessage.rfq_id == rfq.rfq_id)
    )
    stored = result.scalar_one()
    assert stored.user_id == zone_manager.user_id
    assert stored.phase == RfqSubStatus.NEW_RFQ
    assert stored.message == "Need pricing support."


@pytest.mark.asyncio
async def test_discussion_get_filters_by_rfq_and_phase(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="discussion-filter",
        role=UserRole.COMMERCIAL,
        full_name="Filter Owner",
    )
    first_rfq = await _create_rfq(
        db_session,
        creator=creator,
        sub_status=RfqSubStatus.NEW_RFQ,
    )
    second_rfq = await _create_rfq(
        db_session,
        creator=creator,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    db_session.add_all(
        [
            DiscussionMessage(
                rfq_id=first_rfq.rfq_id,
                user_id=creator.user_id,
                phase=RfqSubStatus.NEW_RFQ,
                message="Keep this message",
            ),
            DiscussionMessage(
                rfq_id=first_rfq.rfq_id,
                user_id=creator.user_id,
                phase=RfqSubStatus.POTENTIAL,
                message="Wrong phase",
            ),
            DiscussionMessage(
                rfq_id=second_rfq.rfq_id,
                user_id=creator.user_id,
                phase=RfqSubStatus.NEW_RFQ,
                message="Wrong RFQ",
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/rfq/{first_rfq.rfq_id}/discussion",
        params={"phase": "NEW_RFQ"},
        headers=_headers_for(creator),
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["message"] == "Keep this message"
    assert payload[0]["phase"] == "NEW_RFQ"
    assert payload[0]["author_name"] == "Filter Owner"
    assert payload[0]["author_email"] == creator.email
    assert payload[0]["author_role"] == "COMMERCIAL"


@pytest.mark.asyncio
async def test_discussion_rejects_unauthorized_viewer(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="discussion-owner",
        role=UserRole.COMMERCIAL,
        full_name="Owner",
    )
    stranger = await _create_user(
        db_session,
        prefix="discussion-stranger",
        role=UserRole.COMMERCIAL,
        full_name="Stranger",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    get_response = await client.get(
        f"/api/rfq/{rfq.rfq_id}/discussion",
        params={"phase": "NEW_RFQ"},
        headers=_headers_for(stranger),
    )
    post_response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/discussion",
        json={"phase": "NEW_RFQ", "message": "Please review."},
        headers=_headers_for(stranger),
    )

    assert get_response.status_code == 403
    assert post_response.status_code == 403


@pytest.mark.asyncio
async def test_discussion_rejects_blank_messages(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="discussion-blank",
        role=UserRole.COMMERCIAL,
        full_name="Blank Tester",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        sub_status=RfqSubStatus.NEW_RFQ,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/discussion",
        json={"phase": "NEW_RFQ", "message": "   "},
        headers=_headers_for(creator),
    )

    assert response.status_code == 422
    assert "message" in str(response.json()).lower()
