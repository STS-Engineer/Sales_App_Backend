import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient):
    response = await client.post(
        "/api/auth/register",
        json={"email": "test@avocarbon.com", "password": "securepassword"},
    )
    assert response.status_code == 201
    assert "pending approval" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_register_duplicate(client: AsyncClient):
    # Register once
    await client.post(
        "/api/auth/register",
        json={"email": "dup@avocarbon.com", "password": "pass1"},
    )
    # Register again — should fail
    response = await client.post(
        "/api/auth/register",
        json={"email": "dup@avocarbon.com", "password": "pass2"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_login_pending_user(client: AsyncClient):
    await client.post(
        "/api/auth/register",
        json={"email": "pending@avocarbon.com", "password": "pass"},
    )
    response = await client.post(
        "/api/auth/login",
        json={"email": "pending@avocarbon.com", "password": "pass"},
    )
    assert response.status_code == 403
    assert "pending" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient):
    await client.post(
        "/api/auth/register",
        json={"email": "wrongpw@avocarbon.com", "password": "correct"},
    )
    response = await client.post(
        "/api/auth/login",
        json={"email": "wrongpw@avocarbon.com", "password": "wrong"},
    )
    assert response.status_code == 401
