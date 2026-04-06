import json
import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.potential import Potential
from app.models.user import User, UserRole
from app.routers import chat as formal_chat
from app.routers import chat_potential
from app.routers.auth import create_access_token


def _build_completion(*, content: str | None = None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls)
            )
        ]
    )


def _build_tool_call(name: str, arguments: dict, tool_call_id: str = "call-1"):
    return SimpleNamespace(
        id=tool_call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _queue_openai_responses(monkeypatch, module, responses):
    queued = iter(responses)

    async def _fake_create(*args, **kwargs):
        return next(queued)

    monkeypatch.setattr(module.client.chat.completions, "create", _fake_create)


async def _create_headers(db_session: AsyncSession) -> dict[str, str]:
    email = f"potential-{uuid.uuid4().hex[:8]}@avocarbon.com"
    user = User(
        email=email,
        full_name="Potential Tester",
        role=UserRole.COMMERCIAL,
        is_approved=True,
    )
    user.set_password("secure-password")
    db_session.add(user)
    await db_session.commit()
    return {
        "Authorization": f"Bearer {create_access_token(user.email, user.role.value)}"
    }


@pytest.mark.asyncio
async def test_create_potential_draft_is_hidden_from_dashboard(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _create_headers(db_session)

    create_response = await client.post(
        "/api/rfq",
        json={"chat_mode": "potential"},
        headers=headers,
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["sub_status"] == "POTENTIAL"
    assert created["potential"]["rfq_id"] == created["rfq_id"]

    list_response = await client.get("/api/rfq", headers=headers)
    assert list_response.status_code == 200
    assert all(item["rfq_id"] != created["rfq_id"] for item in list_response.json())

    detail_response = await client.get(
        f"/api/rfq/{created['rfq_id']}",
        headers=headers,
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["potential"]["rfq_id"] == created["rfq_id"]


@pytest.mark.asyncio
async def test_create_direct_rfq_starts_in_new_rfq_and_is_listed(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _create_headers(db_session)

    create_response = await client.post(
        "/api/rfq",
        json={"chat_mode": "rfq"},
        headers=headers,
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["sub_status"] == "NEW_RFQ"
    assert created["potential"] is None

    list_response = await client.get("/api/rfq", headers=headers)
    assert list_response.status_code == 200
    assert any(item["rfq_id"] == created["rfq_id"] for item in list_response.json())


@pytest.mark.asyncio
async def test_potential_chat_generates_incrementing_systematic_ids_case_insensitively(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    headers = await _create_headers(db_session)
    customer_name = f"Nidec {uuid.uuid4().hex[:4]}"
    expected_slug = customer_name.replace(" ", "-").upper()

    first = await client.post("/api/rfq", json={"chat_mode": "potential"}, headers=headers)
    second = await client.post("/api/rfq", json={"chat_mode": "potential"}, headers=headers)
    first_id = first.json()["rfq_id"]
    second_id = second.json()["rfq_id"]

    _queue_openai_responses(
        monkeypatch,
        chat_potential,
        [
            _build_completion(
                tool_calls=[
                    _build_tool_call(
                        "updatePotentialFields",
                        {"fields_to_update": {"customer": customer_name}},
                        "call-1",
                    )
                ]
            ),
            _build_completion(content="Saved."),
            _build_completion(
                tool_calls=[
                    _build_tool_call(
                        "updatePotentialFields",
                        {"fields_to_update": {"customer": customer_name.lower()}},
                        "call-2",
                    )
                ]
            ),
            _build_completion(content="Saved."),
        ],
    )

    first_chat = await client.post(
        "/api/chat/potential",
        json={"rfq_id": first_id, "message": "Customer is Nidec."},
        headers=headers,
    )
    second_chat = await client.post(
        "/api/chat/potential",
        json={"rfq_id": second_id, "message": "Customer is nidec."},
        headers=headers,
    )

    assert first_chat.status_code == 200
    assert second_chat.status_code == 200

    first_detail = await client.get(f"/api/rfq/{first_id}", headers=headers)
    second_detail = await client.get(f"/api/rfq/{second_id}", headers=headers)

    assert (
        first_detail.json()["potential"]["potential_systematic_id"]
        == f"POT-1-{expected_slug}"
    )
    assert (
        second_detail.json()["potential"]["potential_systematic_id"]
        == f"POT-2-{expected_slug}"
    )


@pytest.mark.asyncio
async def test_potential_chat_updates_fields_calculates_margin_and_formal_chat_is_blocked(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    headers = await _create_headers(db_session)
    customer_name = f"Margin Customer {uuid.uuid4().hex[:4]}"
    expected_slug = customer_name.replace(" ", "-").upper()
    create_response = await client.post(
        "/api/rfq",
        json={"chat_mode": "potential"},
        headers=headers,
    )
    rfq_id = create_response.json()["rfq_id"]

    _queue_openai_responses(
        monkeypatch,
        chat_potential,
        [
            _build_completion(
                tool_calls=[
                    _build_tool_call(
                        "updatePotentialFields",
                        {
                            "fields_to_update": {
                                "customerName": customer_name,
                                "salesKeur": 80,
                                "marginPercentage": 25,
                                "industryServed": "Auto",
                            }
                        },
                    )
                ]
            ),
            _build_completion(content="Saved."),
        ],
    )

    chat_response = await client.post(
        "/api/chat/potential",
        json={"rfq_id": rfq_id, "message": "Sales are 80 keur and margin is 25%."},
        headers=headers,
    )
    assert chat_response.status_code == 200
    response_payload = chat_response.json()
    assert response_payload["rfq"]["potential"]["customer"] == customer_name
    assert response_payload["rfq"]["potential"]["industry_served"] == "Auto"

    detail_response = await client.get(f"/api/rfq/{rfq_id}", headers=headers)
    potential = detail_response.json()["potential"]
    assert potential["customer"] == customer_name
    assert potential["industry_served"] == "Auto"
    assert potential["margin_keur"] == 20.0
    assert potential["potential_systematic_id"] == f"POT-1-{expected_slug}"

    blocked_formal_chat = await client.post(
        "/api/chat",
        json={"rfq_id": rfq_id, "message": "Hello", "chat_mode": "rfq"},
        headers=headers,
    )
    assert blocked_formal_chat.status_code == 409


@pytest.mark.asyncio
async def test_proceed_to_formal_rfq_syncs_fields_locks_potential_and_enables_formal_chat(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    headers = await _create_headers(db_session)
    create_response = await client.post(
        "/api/rfq",
        json={"chat_mode": "potential"},
        headers=headers,
    )
    rfq_id = create_response.json()["rfq_id"]

    potential = await db_session.get(Potential, rfq_id)
    potential.customer = "Nidec"
    potential.customer_location = "France"
    potential.application = "Traction motor"
    potential.contact_name = "Jane Doe"
    potential.contact_email = "jane.doe@customer.com"
    potential.contact_phone = "+33 1 23 45 67 89"
    potential.contact_function = "Purchasing Manager"
    await db_session.commit()

    proceed_response = await client.post(
        f"/api/rfq/{rfq_id}/proceed-to-rfq",
        headers=headers,
    )
    assert proceed_response.status_code == 200
    proceeded = proceed_response.json()
    assert proceeded["sub_status"] == "NEW_RFQ"
    assert proceeded["rfq_data"]["customer_name"] == "Nidec"
    assert proceeded["rfq_data"]["application"] == "Traction motor"
    assert proceeded["rfq_data"]["contact_name"] == "Jane Doe"
    assert proceeded["rfq_data"]["contact_email"] == "jane.doe@customer.com"
    assert proceeded["rfq_data"]["contact_phone"] == "+33 1 23 45 67 89"
    assert proceeded["rfq_data"]["contact_role"] == "Purchasing Manager"
    assert proceeded["rfq_data"]["country"] == "France"

    blocked_potential_chat = await client.post(
        "/api/chat/potential",
        json={"rfq_id": rfq_id, "message": "Can I still edit Potential?"},
        headers=headers,
    )
    assert blocked_potential_chat.status_code == 409

    _queue_openai_responses(
        monkeypatch,
        formal_chat,
        [_build_completion(content="Formal RFQ chat is active.")],
    )
    formal_chat_response = await client.post(
        "/api/chat",
        json={"rfq_id": rfq_id, "message": "Let's continue with the RFQ.", "chat_mode": "rfq"},
        headers=headers,
    )
    assert formal_chat_response.status_code == 200
    assert formal_chat_response.json()["response"] == "Formal RFQ chat is active."


@pytest.mark.asyncio
async def test_proceed_to_formal_rfq_keeps_existing_country_value(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _create_headers(db_session)
    create_response = await client.post(
        "/api/rfq",
        json={"chat_mode": "potential", "rfq_data": {"country": "Germany"}},
        headers=headers,
    )
    rfq_id = create_response.json()["rfq_id"]

    potential = await db_session.get(Potential, rfq_id)
    potential.customer = "Nidec"
    potential.customer_location = "France"
    potential.application = "Traction motor"
    potential.contact_name = "Jane Doe"
    potential.contact_email = "jane.doe@customer.com"
    potential.contact_phone = "+33 1 23 45 67 89"
    potential.contact_function = "Purchasing Manager"
    await db_session.commit()

    proceed_response = await client.post(
        f"/api/rfq/{rfq_id}/proceed-to-rfq",
        headers=headers,
    )

    assert proceed_response.status_code == 200
    assert proceed_response.json()["rfq_data"]["country"] == "Germany"
