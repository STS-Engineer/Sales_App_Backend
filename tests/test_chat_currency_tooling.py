import inspect
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/rfq_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.routers import chat


def test_tools_include_live_fx_lookup():
    assert "get_eur_exchange_rate" in {
        tool["function"]["name"] for tool in chat.TOOLS
    }


def test_normalize_tool_arguments_maps_currency_aliases():
    normalized = chat._normalize_tool_arguments(
        "get_eur_exchange_rate",
        {
            "currency": "usd",
            "currencyCode": "gbp",
            "from_currency": "mxn",
        },
    )

    assert normalized["currency_code"] == "gbp"


def test_normalize_tool_arguments_maps_retrieve_zone_manager_delivery_zone_alias():
    normalized = chat._normalize_tool_arguments(
        "retrieveZoneManager",
        {"toTotal": 500, "productLine": "BRU", "deliveryZone": "Europe"},
    )

    assert normalized["to_total"] == 500
    assert normalized["product_line_acronym"] == "BRU"
    assert normalized["delivery_zone"] == "Europe"


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_fx_payload(monkeypatch):
    async def _fake_get_rate(currency_code):
        assert currency_code == "USD"
        return 0.91

    monkeypatch.setattr(chat, "get_eur_exchange_rate", _fake_get_rate)

    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "fx-1",
                "name": "get_eur_exchange_rate",
                "arguments": {"currency_code": "usd"},
            }
        ],
        http_client=None,
        db=None,
        rfq=SimpleNamespace(created_by_email="owner@example.com"),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data={},
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload == {
        "currency_code": "USD",
        "eur_rate": 0.91,
        "fallback_used": False,
    }


@pytest.mark.asyncio
async def test_execute_tool_calls_flags_fx_fallback(monkeypatch):
    async def _fake_get_rate(currency_code):
        assert currency_code == "MXN"
        return 1.0

    monkeypatch.setattr(chat, "get_eur_exchange_rate", _fake_get_rate)

    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "fx-2",
                "name": "get_eur_exchange_rate",
                "arguments": {"currency_code": "mxn"},
            }
        ],
        http_client=None,
        db=None,
        rfq=SimpleNamespace(created_by_email="owner@example.com"),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data={},
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["currency_code"] == "MXN"
    assert payload["eur_rate"] == 1.0
    assert payload["fallback_used"] is True


class _FakeResult:
    def __init__(self, matrix):
        self._matrix = matrix

    def scalar_one_or_none(self):
        return self._matrix


class _FakeDb:
    def __init__(self, matrix):
        self._matrix = matrix

    async def execute(self, query):
        return _FakeResult(self._matrix)


def _build_matrix():
    return SimpleNamespace(
        n3_kam_limit=250,
        n2_zone_limit=750,
        n1_vp_limit=1500,
    )


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_zone_manager_payload_with_canonical_zone():
    extracted_data = {}
    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-1",
                "name": "retrieveZoneManager",
                "arguments": {
                    "to_total": 500,
                    "product_line_acronym": "BRU",
                    "delivery_zone": "America",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        rfq=SimpleNamespace(created_by_email="owner@example.com"),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["validator_role"] == "Zone Manager"
    assert payload["zone_manager_email"] == "dean.hayward@avocarbon.com"
    assert payload["delivery_zone"] == "amerique"
    assert extracted_data["delivery_zone"] == "amerique"


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_error_for_unknown_zone_manager_zone():
    extracted_data = {}
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-2",
                "name": "retrieveZoneManager",
                "arguments": {
                    "to_total": 500,
                    "product_line_acronym": "BRU",
                    "delivery_zone": "antarctica",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        rfq=SimpleNamespace(created_by_email="owner@example.com"),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert "error" in payload
    assert payload["approved_delivery_zones"] == [
        "asie est",
        "asie sud",
        "europe",
        "amerique",
    ]
    assert extracted_data == {}


def test_system_prompt_includes_dimension_fx_and_delivery_zone_instructions():
    assert "Always store dimension data in mm." in chat.SYSTEM_PROMPT
    assert "Target Price and quoted currency" in chat.SYSTEM_PROMPT
    assert "MUST call `get_eur_exchange_rate`" in chat.SYSTEM_PROMPT
    assert "Ask the user to restate the Target Price directly in EUR" in chat.SYSTEM_PROMPT
    assert "exactly one of these 4 approved `delivery_zone` strings" in chat.SYSTEM_PROMPT
    assert "France -> europe, Mexico -> amerique, China -> asie est, India -> asie sud" in chat.SYSTEM_PROMPT
    assert "Any `delivery_zone` you send through `updateFormFields` MUST exactly match one of the 4 approved strings" in chat.SYSTEM_PROMPT


def test_dynamic_prompt_reinforces_delivery_zone_sync_rules():
    source = inspect.getsource(chat.handle_chat)

    assert "frontend form stays synchronized with the latest data" in source
    assert "approved values before calling `updateFormFields`: `asie est`, `asie sud`, `europe`, `amerique`" in source
