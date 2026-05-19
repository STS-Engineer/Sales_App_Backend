import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product_line_routing import ProductLineRouting
from app.models.user import User, UserRole
from app.models.validation_matrix import ValidationMatrix
from app.routers.auth import create_access_token


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    role: UserRole,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=f"{prefix.title()} User",
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


async def _ensure_matrix(
    db_session: AsyncSession,
    *,
    product_line: str = "Brushes",
    acronym: str = "BRU",
) -> None:
    existing = (
        await db_session.execute(
            select(ValidationMatrix).where(ValidationMatrix.acronym == acronym)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    db_session.add(
        ValidationMatrix(
            product_line=product_line,
            acronym=acronym,
            n3_kam_limit=10,
            n2_zone_limit=20,
            n1_vp_limit=30,
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_owner_can_crud_product_line_routing(
    client: AsyncClient,
    db_session: AsyncSession,
):
    owner = await _create_user(
        db_session,
        prefix="routing-owner",
        role=UserRole.OWNER,
    )
    await _ensure_matrix(db_session, product_line="Brushes", acronym="BRU")

    create_response = await client.post(
        "/api/owner/routing-config",
        json={
            "product_line": "Brushes",
            "role": "COSTING",
            "email": "costing.brushes@avocarbon.com",
        },
        headers=_headers_for(owner),
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["product_line"] == "Brushes"
    assert created["role"] == "COSTING"
    assert created["email"] == "costing.brushes@avocarbon.com"
    assert created["updated_at"]

    list_response = await client.get(
        "/api/owner/routing-config",
        headers=_headers_for(owner),
    )
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    filtered_response = await client.get(
        "/api/owner/routing-config?product_line=Brushes",
        headers=_headers_for(owner),
    )
    assert filtered_response.status_code == 200
    assert len(filtered_response.json()) == 1

    update_response = await client.put(
        f"/api/owner/routing-config/{created['id']}",
        json={
            "product_line": "Brushes",
            "role": "PLM",
            "email": "plm.brushes@avocarbon.com",
        },
        headers=_headers_for(owner),
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["role"] == "PLM"
    assert updated["email"] == "plm.brushes@avocarbon.com"

    rows = (
        await db_session.execute(select(ProductLineRouting))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].role.value == "PLM"

    delete_response = await client.delete(
        f"/api/owner/routing-config/{created['id']}",
        headers=_headers_for(owner),
    )
    assert delete_response.status_code == 204

    remaining = (
        await db_session.execute(select(ProductLineRouting))
    ).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_routing_config_requires_owner_role(
    client: AsyncClient,
    db_session: AsyncSession,
):
    user = await _create_user(
        db_session,
        prefix="routing-commercial",
        role=UserRole.COMMERCIAL,
    )

    response = await client.get(
        "/api/owner/routing-config",
        headers=_headers_for(user),
    )

    assert response.status_code == 403
