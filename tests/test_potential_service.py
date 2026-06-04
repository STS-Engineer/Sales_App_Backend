import json
from types import SimpleNamespace

import pytest

from app.models.potential import Potential
from app.models.rfq import Rfq
from app.routers import chat_potential
from app.schemas.potential import normalize_potential_update_payload
from app.services.potential import calculate_margin_keur, slugify_customer_name, sync_potential_to_rfq_data


def test_slugify_customer_name_normalizes_spaces_and_special_characters():
    assert slugify_customer_name("Nidec Corp") == "NIDEC-CORP"
    assert slugify_customer_name("Nidéç / Corp") == "NIDEC-CORP"


def test_calculate_margin_keur_returns_two_decimal_float():
    assert calculate_margin_keur(80, 25) == 20.0
    assert calculate_margin_keur("125,5", "10") == 12.55


def test_sync_potential_to_rfq_data_preserves_existing_country():
    potential = Potential(
        customer="Nidec",
        customer_location="France",
        application="Traction motor",
        contact_name="Jane Doe",
        contact_email="jane.doe@customer.com",
        contact_phone="+33 1 23 45 67 89",
        contact_function="Purchasing Manager",
    )

    synced = sync_potential_to_rfq_data(
        potential,
        {"country": "Germany", "existing_key": "keep-me"},
    )

    assert synced["customer_name"] == "Nidec"
    assert synced["application"] == "Traction motor"
    assert synced["contact_name"] == "Jane Doe"
    assert synced["contact_email"] == "jane.doe@customer.com"
    assert synced["contact_phone"] == "+33 1 23 45 67 89"
    assert synced["contact_role"] == "Purchasing Manager"
    assert synced["country"] == "Germany"
    assert synced["existing_key"] == "keep-me"


def test_normalize_potential_update_payload_accepts_camel_case_and_frontend_aliases():
    normalized, ignored = normalize_potential_update_payload(
        {
            "customerName": "Nidec",
            "customerLocation": "France",
            "contactRole": "Purchasing Manager",
            "industryServed": "Auto",
            "plannedProductType": "Brushes",
            "potentialBusinessSalesKeur": 80,
            "potentialBusinessMarginPercent": 25,
            "potentialRiskDoAssessment": "Spend money for nothing",
        }
    )

    assert ignored == []
    assert normalized["customer"] == "Nidec"
    assert normalized["customer_location"] == "France"
    assert normalized["contact_function"] == "Purchasing Manager"
    assert normalized["industry_served"] == "Auto"
    assert normalized["planned_product_type"] == "Brushes"
    assert normalized["sales_keur"] == 80.0
    assert normalized["margin_percentage"] == 25.0
    assert normalized["risks_to_do"] == "Spend money for nothing"


def test_build_dynamic_prompt_prioritizes_guided_questions_over_product_count():
    potential = Potential()
    rfq = Rfq(rfq_data={})
    next_question = "Who is the customer and where are they located?"

    prompt = chat_potential._build_dynamic_prompt(
        potential=potential,
        rfq=rfq,
        next_question=next_question,
        missing_fields=["customer", "customer_location"],
    )

    assert "Do NOT start by asking how many part numbers/products are included in the request." in prompt
    assert "Only collect product rows if the user voluntarily provides product details or explicitly asks to add products." in prompt
    assert f"Current next question:\n{next_question}" in prompt
    assert "First, ask the user how many part numbers/products are included in this request." not in prompt


def test_slice_history_for_llm_keeps_assistant_tool_call_before_tool_message():
    history = [
        {"role": "assistant", "content": "Greeting"},
        {"role": "user", "content": "First answer"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "checkGroupeExistence",
                        "arguments": "{\"customer_name\": \"Valeo\"}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "checkGroupeExistence",
            "content": "{\"exists\": false}",
        },
        {"role": "assistant", "content": "Saved customer. Anything else?"},
    ]

    sliced_history = chat_potential._slice_history_for_llm(history, max_messages=2)

    assert [entry["role"] for entry in sliced_history] == ["assistant", "tool", "assistant"]
    assert sliced_history[0]["tool_calls"][0]["id"] == "call-1"


def test_truncate_potential_chat_history_for_edit_keeps_messages_before_target_user():
    history = [
        {"role": "assistant", "content": "Greeting"},
        {"role": "user", "content": "Original answer"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {
                        "name": "updatePotentialFields",
                        "arguments": "{\"fields_to_update\": {\"customer\": \"Valeo\"}}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "name": "updatePotentialFields",
            "content": "{\"success\": true}",
        },
        {"role": "assistant", "content": "Next question"},
        {"role": "user", "content": "Second answer"},
    ]

    truncated_history = chat_potential._truncate_potential_chat_history_for_edit(
        history,
        3,
    )

    assert [entry["role"] for entry in truncated_history] == [
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert truncated_history[-1]["content"] == "Next question"


@pytest.mark.asyncio
async def test_generate_potential_response_supports_verify_then_save_in_same_turn(
    monkeypatch,
):
    captured_system_prompts = []
    responses = iter(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id="call-1",
                                    function=SimpleNamespace(
                                        name="checkGroupeExistence",
                                        arguments=json.dumps({"customer_name": "Nidec"}),
                                    ),
                                )
                            ],
                        )
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id="call-2",
                                    function=SimpleNamespace(
                                        name="updatePotentialFields",
                                        arguments=json.dumps(
                                            {
                                                "fields_to_update": {
                                                    "customer": "Nidec",
                                                    "customer_location": "France",
                                                }
                                            }
                                        ),
                                    ),
                                )
                            ],
                        )
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="Saved. What is the application?",
                            tool_calls=None,
                        )
                    )
                ]
            ),
        ]
    )

    async def _fake_create(*args, **kwargs):
        captured_system_prompts.append(kwargs["messages"][0]["content"])
        return next(responses)

    async def _fake_execute_tool_calls(**kwargs):
        tool_calls = kwargs["tool_calls"]
        tool_calls_used = kwargs["tool_calls_used"]
        potential = kwargs["potential"]
        for tool_call in tool_calls:
            tool_calls_used.append(tool_call["name"])
            if tool_call["name"] == "updatePotentialFields":
                fields_to_update = tool_call["arguments"].get("fields_to_update", {})
                potential.customer = fields_to_update.get("customer")
                potential.customer_location = fields_to_update.get("customer_location")
        return [
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_call["name"],
                "content": json.dumps({"success": True}),
            }
            for tool_call in tool_calls
        ]

    monkeypatch.setattr(chat_potential.client.chat.completions, "create", _fake_create)
    monkeypatch.setattr(chat_potential, "_execute_tool_calls", _fake_execute_tool_calls)

    history = []
    potential = Potential()
    rfq = Rfq(rfq_data={})
    initial_prompt = chat_potential._build_dynamic_prompt(
        potential=potential,
        rfq=rfq,
        next_question="Who is the customer and where are they located?",
        missing_fields=["customer", "customer_location"],
    )
    messages_for_llm = [{"role": "system", "content": initial_prompt}]
    final_text, tool_calls_used = await chat_potential._generate_potential_response(
        db=None,
        rfq=rfq,
        potential=potential,
        history=history,
        messages_for_llm=messages_for_llm,
    )

    assert final_text == "Saved. What is the application?"
    assert tool_calls_used == ["checkGroupeExistence", "updatePotentialFields"]
    assert [entry["role"] for entry in history] == ["assistant", "tool", "assistant", "tool"]
    assert "Current next question:\nWho is the customer and where are they located?" in captured_system_prompts[0]
    assert "Current next question:\nWhat is the application, and what industry is this product serving (Auto, Consumer, Industry...)?" in captured_system_prompts[-1]
