import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.routers.auth import create_access_token, create_refresh_token
from app.security import create_password_reset_token, decode_token


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


@pytest.mark.asyncio
async def test_forgot_password_returns_generic_message_and_sends_email(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    user = await _create_user(db_session, prefix="forgot-password")
    sent_payload = {}

    def _fake_send_password_reset_email(user_email, full_name, reset_link, expires_minutes):
        sent_payload.update(
            {
                "user_email": user_email,
                "full_name": full_name,
                "reset_link": reset_link,
                "expires_minutes": expires_minutes,
            }
        )
        return True

    monkeypatch.setattr(
        "app.routers.auth.emails.send_password_reset_email",
        _fake_send_password_reset_email,
    )

    response = await client.post(
        "/api/auth/forgot-password",
        json={"email": user.email},
    )

    assert response.status_code == 200
    assert "if an account exists" in response.json()["message"].lower()
    assert sent_payload["user_email"] == user.email
    assert sent_payload["full_name"] == user.full_name
    assert sent_payload["expires_minutes"] > 0

    parsed_link = urlparse(sent_payload["reset_link"])
    token = parse_qs(parsed_link.query)["token"][0]
    payload = decode_token(token)
    assert payload["sub"] == user.email
    assert payload["token_type"] == "password_reset"
    assert payload["pwd"]


@pytest.mark.asyncio
async def test_forgot_password_unknown_email_does_not_send_email(
    client: AsyncClient,
    monkeypatch,
):
    sent_calls = []

    def _fake_send_password_reset_email(*args, **kwargs):
        sent_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(
        "app.routers.auth.emails.send_password_reset_email",
        _fake_send_password_reset_email,
    )

    response = await client.post(
        "/api/auth/forgot-password",
        json={"email": "unknown@avocarbon.com"},
    )

    assert response.status_code == 200
    assert "if an account exists" in response.json()["message"].lower()
    assert sent_calls == []


@pytest.mark.asyncio
async def test_validate_reset_password_token_accepts_valid_token(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(db_session, prefix="validate-reset")
    token = create_password_reset_token(user.email, user.password_hash)

    response = await client.get(
        "/api/auth/reset-password/validate",
        params={"token": token},
    )

    assert response.status_code == 200
    assert "valid" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_reset_password_updates_password_and_allows_login(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(
        db_session,
        prefix="reset-success",
        password="old-password",
    )
    token = create_password_reset_token(user.email, user.password_hash)

    response = await client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "new-password-123"},
    )

    assert response.status_code == 200
    assert "reset" in response.json()["message"].lower()

    old_login = await client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "old-password"},
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "new-password-123"},
    )
    assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_reset_password_rejects_stale_token_after_password_change(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(
        db_session,
        prefix="reset-stale",
        password="before-change",
    )
    token = create_password_reset_token(user.email, user.password_hash)
    user.set_password("changed-in-between")
    await db_session.commit()

    response = await client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "new-password-123"},
    )

    assert response.status_code == 400
    assert "invalid or has expired" in response.json()["detail"].lower()
