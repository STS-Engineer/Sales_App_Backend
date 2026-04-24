import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.routers.auth import create_access_token, create_refresh_token


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    password: str = "secure-password",
    role: UserRole = UserRole.COMMERCIAL,
    is_approved: bool = True,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=f"{prefix.title()} User",
        role=role,
        is_approved=is_approved,
    )
    user.set_password(password)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient):
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "test@avocarbon.com",
            "password": "securepassword",
            "full_name": "Test User",
        },
    )
    assert response.status_code == 201
    assert "pending approval" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_register_duplicate(client: AsyncClient):
    await client.post(
        "/api/auth/register",
        json={
            "email": "dup@avocarbon.com",
            "password": "pass1",
            "full_name": "Duplicate User",
        },
    )
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "dup@avocarbon.com",
            "password": "pass2",
            "full_name": "Duplicate User",
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_login_pending_user(client: AsyncClient):
    await client.post(
        "/api/auth/register",
        json={
            "email": "pending@avocarbon.com",
            "password": "pass",
            "full_name": "Pending User",
        },
    )
    response = await client.post(
        "/api/auth/login",
        json={"email": "pending@avocarbon.com", "password": "pass"},
    )
    assert response.status_code == 403
    assert "pending" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient, db_session: AsyncSession):
    user = await _create_user(
        db_session,
        prefix="wrongpw",
        password="correct",
        is_approved=True,
    )
    response = await client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "wrong"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_returns_access_and_refresh_tokens(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(
        db_session,
        prefix="login",
        password="correct-password",
        is_approved=True,
    )

    response = await client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "correct-password"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"]
    assert payload["refresh_token"]
    assert payload["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_refresh_returns_new_access_token(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(db_session, prefix="refresh")
    refresh_token = create_refresh_token(user.email, user.role.value)

    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"]
    assert payload["token_type"] == "bearer"
    assert "refresh_token" not in payload


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(db_session, prefix="access-as-refresh")
    access_token = create_access_token(user.email, user.role.value)

    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": access_token},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rejects_user_that_is_no_longer_approved(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(db_session, prefix="refresh-pending")
    refresh_token = create_refresh_token(user.email, user.role.value)
    user.is_approved = False
    await db_session.commit()

    response = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_rejects_refresh_token(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(db_session, prefix="refresh-bearer")
    refresh_token = create_refresh_token(user.email, user.role.value)

    response = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert response.status_code == 401
