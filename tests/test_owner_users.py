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


async def _find_user(db_session: AsyncSession, user_id: str) -> User | None:
    result = await db_session.execute(select(User).where(User.user_id == user_id))
    return result.scalar_one_or_none()


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
async def test_owner_can_promote_user_to_owner_and_auto_approve_account(
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
        prefix="owner-security",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    commercial_user = await _create_user(
        db_session,
        prefix="commercial-security",
        role=UserRole.COMMERCIAL,
        is_approved=False,
        full_name="Commercial User",
    )

    response = await client.put(
        f"/api/owner/users/{commercial_user.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "OWNER", "is_approved": False},
    )

    assert response.status_code == 200
    updated_user = await _get_user(db_session, commercial_user.user_id)

    assert updated_user.role == UserRole.OWNER
    assert updated_user.is_approved is True
    assert len(email_calls) == 1
    assert email_calls[0][0][0] == commercial_user.email
    assert email_calls[0][0][1] == "Owner"


@pytest.mark.asyncio
async def test_owner_can_edit_owner_accounts_when_another_owner_exists(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-edit-owner",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    owner_target = await _create_user(
        db_session,
        prefix="owner-target-security",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner Target",
    )

    response = await client.put(
        f"/api/owner/users/{owner_target.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "COMMERCIAL"},
    )

    assert response.status_code == 200
    updated_user = await _get_user(db_session, owner_target.user_id)

    assert updated_user.role == UserRole.COMMERCIAL
    assert updated_user.is_approved is True


@pytest.mark.asyncio
async def test_owner_cannot_demote_last_remaining_owner(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-last-role",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Only Owner",
    )

    response = await client.put(
        f"/api/owner/users/{owner.user_id}/role",
        headers=_headers_for(owner),
        json={"role": "COMMERCIAL"},
    )

    assert response.status_code == 400
    assert "At least one owner account must remain active." in response.json()["detail"]


@pytest.mark.asyncio
async def test_owner_can_delete_approved_user(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-delete-approved",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    approved_user = await _create_user(
        db_session,
        prefix="approved-delete",
        role=UserRole.PLM,
        is_approved=True,
        full_name="Approved User",
    )

    response = await client.delete(
        f"/api/owner/users/{approved_user.user_id}",
        headers=_headers_for(owner),
    )

    assert response.status_code == 204
    assert await _find_user(db_session, approved_user.user_id) is None


@pytest.mark.asyncio
async def test_owner_can_delete_owner_when_another_owner_exists(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-delete-owner",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner User",
    )
    owner_target = await _create_user(
        db_session,
        prefix="owner-delete-target",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Owner Target",
    )

    response = await client.delete(
        f"/api/owner/users/{owner_target.user_id}",
        headers=_headers_for(owner),
    )

    assert response.status_code == 204
    assert await _find_user(db_session, owner_target.user_id) is None


@pytest.mark.asyncio
async def test_owner_cannot_delete_last_remaining_owner(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-delete-last",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Only Owner",
    )

    response = await client.delete(
        f"/api/owner/users/{owner.user_id}",
        headers=_headers_for(owner),
    )

    assert response.status_code == 400
    assert "At least one owner account must remain active." in response.json()["detail"]


@pytest.mark.asyncio
async def test_non_owner_cannot_delete_user(
    client: AsyncClient,
    db_session: AsyncSession,
):
    commercial_user = await _create_user(
        db_session,
        prefix="commercial-delete-actor",
        role=UserRole.COMMERCIAL,
        is_approved=True,
        full_name="Commercial User",
    )
    target_user = await _create_user(
        db_session,
        prefix="delete-target",
        role=UserRole.PLM,
        is_approved=True,
        full_name="Target User",
    )

    response = await client.delete(
        f"/api/owner/users/{target_user.user_id}",
        headers=_headers_for(commercial_user),
    )

    assert response.status_code == 403


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


@pytest.mark.asyncio
async def test_legacy_users_delete_route_uses_same_owner_guard(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="owner-legacy-delete",
        role=UserRole.OWNER,
        is_approved=True,
        full_name="Only Owner",
    )

    response = await client.delete(
        f"/api/users/{owner.user_id}",
        headers=_headers_for(owner),
    )

    assert response.status_code == 400
    assert "At least one owner account must remain active." in response.json()["detail"]
