import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.routers.auth import create_access_token


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    role: UserRole,
    is_approved: bool,
    full_name: str,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=full_name,
        role=role,
        is_approved=is_approved,
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


async def _get_user(db_session: AsyncSession, user_id: str) -> User:
    result = await db_session.execute(select(User).where(User.user_id == user_id))
    return result.scalar_one()


@pytest.mark.asyncio
async def test_owner_can_list_all_users_including_owners(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-list",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    pending_user = await _create_user(
        db_session,
        prefix="pending-list",
        role=UserRole.COMMERCIAL,
        is_approved=False,
        full_name="Pending User",
    )
    approved_user = await _create_user(
        db_session,
        prefix="approved-list",
        role=UserRole.PLM,
        is_approved=True,
        full_name="Approved User",
    )
    owner_target = await _create_user(
        db_session,
        prefix="owner-target",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner Target",
    )

    response = await client.get("/api/owner/users", headers=_headers_for(owner))

    assert response.status_code == 200
    payload = response.json()
    returned_emails = {entry["email"] for entry in payload}

    assert pending_user.email in returned_emails
    assert approved_user.email in returned_emails
    assert owner.email in returned_emails
    assert owner_target.email in returned_emails
    assert sum(1 for entry in payload if entry["role"] == "OWNER") == 2


@pytest.mark.asyncio
async def test_owner_can_approve_pending_user_and_assign_role(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    email_calls = []
    monkeypatch.setattr(
        "app.services.user_admin.emails.send_approval_email",
        lambda *args, **kwargs: email_calls.append((args, kwargs)),
    )

    owner = await _create_user(
        db_session,
        prefix="owner-approve",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    pending_user = await _create_user(
        db_session,
        prefix="pending-approve",
        role=UserRole.COMMERCIAL,
        is_approved=False,
        full_name="Pending User",
    )

    response = await client.put(
        f"/api/owner/users/{pending_user.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "ZONE_MANAGER", "is_approved": True},
    )

    assert response.status_code == 200
    updated_user = await _get_user(db_session, pending_user.user_id)

    assert updated_user.role == UserRole.ZONE_MANAGER
    assert updated_user.is_approved is True
    assert response.json()["role"] == "ZONE_MANAGER"
    assert response.json()["is_approved"] is True
    assert len(email_calls) == 1
    assert email_calls[0][0][0] == pending_user.email
    assert email_calls[0][0][1] == "Zone Manager"


@pytest.mark.asyncio
async def test_owner_can_edit_approved_user_role_without_changing_approval(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    email_calls = []
    monkeypatch.setattr(
        "app.services.user_admin.emails.send_approval_email",
        lambda *args, **kwargs: email_calls.append((args, kwargs)),
    )

    owner = await _create_user(
        db_session,
        prefix="owner-edit",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    approved_user = await _create_user(
        db_session,
        prefix="approved-edit",
        role=UserRole.COMMERCIAL,
        is_approved=True,
        full_name="Approved User",
    )

    response = await client.put(
        f"/api/owner/users/{approved_user.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "PLM"},
    )

    assert response.status_code == 200
    updated_user = await _get_user(db_session, approved_user.user_id)

    assert updated_user.role == UserRole.PLM
    assert updated_user.is_approved is True
    assert len(email_calls) == 0


@pytest.mark.asyncio
async def test_owner_can_keep_pending_user_unapproved_while_updating_role(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    email_calls = []
    monkeypatch.setattr(
        "app.services.user_admin.emails.send_approval_email",
        lambda *args, **kwargs: email_calls.append((args, kwargs)),
    )

    owner = await _create_user(
        db_session,
        prefix="owner-pending",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    pending_user = await _create_user(
        db_session,
        prefix="pending-hold",
        role=UserRole.COMMERCIAL,
        is_approved=False,
        full_name="Pending User",
    )

    response = await client.put(
        f"/api/owner/users/{pending_user.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "PLANT_MANAGER", "is_approved": False},
    )

    assert response.status_code == 200
    updated_user = await _get_user(db_session, pending_user.user_id)

    assert updated_user.role == UserRole.PLANT_MANAGER
    assert updated_user.is_approved is False
    assert len(email_calls) == 0


@pytest.mark.asyncio
async def test_non_owner_cannot_update_user_role(
    client: AsyncClient,
    db_session: AsyncSession,
):
    commercial_user = await _create_user(
        db_session,
        prefix="commercial-actor",
        role=UserRole.COMMERCIAL,
        is_approved=True,
        full_name="Commercial User",
    )
    pending_user = await _create_user(
        db_session,
        prefix="pending-target",
        role=UserRole.PLM,
        is_approved=False,
        full_name="Pending User",
    )

    response = await client.put(
        f"/api/owner/users/{pending_user.user_id}/role",
        headers=_headers_for(commercial_user),
        json={"role": "ZONE_MANAGER", "is_approved": True},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_owner_update_returns_404_for_unknown_user(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-missing",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )

    response = await client.put(
        f"/api/owner/users/{uuid.uuid4()}/role",
        headers=_headers_for(owner),
        json={"role": "PLM"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_owner_cannot_assign_owner_role_or_edit_owner_accounts(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-security",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    commercial_user = await _create_user(
        db_session,
        prefix="commercial-security",
        role=UserRole.COMMERCIAL,
        is_approved=True,
        full_name="Commercial User",
    )
    owner_target = await _create_user(
        db_session,
        prefix="owner-target-security",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner Target",
    )

    assign_owner_response = await client.put(
        f"/api/owner/users/{commercial_user.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "OWNER"},
    )
    edit_owner_response = await client.put(
        f"/api/owner/users/{owner_target.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "COMMERCIAL"},
    )

    assert assign_owner_response.status_code == 400
    assert "Owner role cannot be assigned" in assign_owner_response.json()["detail"]
    assert edit_owner_response.status_code == 400
    assert "Owner accounts cannot be edited" in edit_owner_response.json()["detail"]


@pytest.mark.asyncio
async def test_legacy_users_role_route_uses_same_approval_behavior(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    email_calls = []
    monkeypatch.setattr(
        "app.services.user_admin.emails.send_approval_email",
        lambda *args, **kwargs: email_calls.append((args, kwargs)),
    )

    owner = await _create_user(
        db_session,
        prefix="owner-legacy",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    pending_user = await _create_user(
        db_session,
        prefix="pending-legacy",
        role=UserRole.COMMERCIAL,
        is_approved=False,
        full_name="Pending User",
    )

    response = await client.put(
        f"/api/users/{pending_user.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "ZONE_MANAGER", "is_approved": False},
    )

    assert response.status_code == 200
    updated_user = await _get_user(db_session, pending_user.user_id)

    assert updated_user.role == UserRole.ZONE_MANAGER
    assert updated_user.is_approved is False
    assert len(email_calls) == 0
