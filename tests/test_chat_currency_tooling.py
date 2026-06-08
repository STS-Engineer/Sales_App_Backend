import inspect
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/rfq_test",
)
os.environ.setdefault(
    "DATABASE_URL3",
    "postgresql://user:password@localhost:5432/rfq_fx_test",
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

    assert normalized["product_line_acronym"] == "BRU"
    assert normalized["delivery_zone"] == "Europe"
    assert "to_total" not in normalized


def test_sanitize_assistant_text_drops_raw_json_payload_variants():
    assert (
        chat._sanitize_assistant_text(
            '{"fields_to_update": {"customer_name": "Nidec"}}'
        )
        == ""
    )
    assert (
        chat._sanitize_assistant_text(
            '{"fieldstoupdate": {"customer_name": "Nidec"}}'
        )
        == ""
    )
    assert chat._sanitize_assistant_text('["draft", "payload"]') == ""


def test_sanitize_assistant_text_removes_json_blocks_and_preserves_prose():
    content = (
        "I saved the latest details.\n\n"
        "```json\n"
        '{"fields_to_update": {"customer_name": "Nidec"}}\n'
        "```\n\n"
        "Please continue with the next missing fields."
    )

    assert chat._sanitize_assistant_text(content) == "I saved the latest details."


def test_sanitize_assistant_text_removes_leading_tool_payload_and_keeps_summary():
    content = (
        '{"fieldstoupdate":{"products":[{"partnumber":"p25845","revisionlevel":"rev01","quantity":10000.0,'
        '"targetprice":1000.0,"currency":"EUR","targetpriceisestimated":true,"targetto":10000000.0}],'
        '"totaltargetto":10000000.0,"tototal":"10000.0","tototallocal":null,'
        '"zonemanageremail":"taha.khiari@avocarbon.com","validatorrole":"CEO"}}\n\n'
        "Total target TO: 10,000,000\n"
        "Total turnover (kEUR): 10,000.0\n"
        "Delivery zone: Europe\n"
        "Validator: CEO (taha.khiari@avocarbon.com)\n"
        "Do you want to submit this RFQ for validation?\n\n"
        "Yes\n"
        "No"
    )

    assert chat._sanitize_assistant_text(content) == (
        "Total target TO: 10,000,000\n"
        "Total turnover (kEUR): 10,000.0\n"
        "Delivery zone: Europe\n"
        "Validator: CEO (taha.khiari@avocarbon.com)\n"
        "Do you want to submit this RFQ for validation?\n\n"
        "Yes\n"
        "No"
    )


def test_sanitize_assistant_text_rewrites_bare_field_label_into_question():
    assert chat._sanitize_assistant_text("Contact phone") == (
        "What is the Contact phone number?"
    )


def test_sanitize_assistant_text_rewrites_field_label_with_options_into_question():
    content = (
        "Delivery zone\n\n"
        "Europe\n"
        "Africa\n"
        "India"
    )

    assert chat._sanitize_assistant_text(content) == (
        "Which delivery zone applies to this RFQ?\n\n"
        "Europe\n"
        "Africa\n"
        "India"
    )


def test_sanitize_assistant_text_removes_failed_to_parse_json_line():
    content = (
        "Failed to parse as JSON: unexpected character: line 1 column 1 (char 0)\n\n"
        "New customer. It will be added to the database later after we get the contact details.\n"
        "What is the Application?"
    )

    assert chat._sanitize_assistant_text(content) == (
        "New customer. It will be added to the database later after we get the contact details.\n"
        "What is the Application?"
    )


def test_sanitize_assistant_text_removes_update_saved_filler_and_keeps_question():
    content = (
        "**Update saved.**\n\n"
        "- I've processed the latest information.\n"
        "- Please continue with the next missing fields.\n\n"
        "What is the Application?"
    )

    assert chat._sanitize_assistant_text(content) == "What is the Application?"


def test_build_user_facing_fallback_text_explains_internal_avocarbon_contact_rejection():
    rfq = SimpleNamespace(
        sub_status="NEW_RFQ",
        document_type=chat.RfqDocumentType.RFQ,
    )

    text = chat._build_user_facing_fallback_text(
        rfq=rfq,
        chat_mode="rfq",
        extracted_data={
            "customer_name": "Bosch",
            "application": "Motor",
            "product_name": "Brush Holder",
            "project_name": "Proj-1",
            "rfq_files": [{"name": "drawing.pdf"}],
            "products": [
                {
                    "part_number": "PN-001",
                    "quantity": 1000,
                    "target_price": 1.5,
                    "currency": "EUR",
                    "target_price_is_estimated": True,
                }
            ],
            "delivery_zone": "Europe",
            "delivery_plant": "Plant A",
            "country": "France",
            "po_date": "2027-01-01",
            "ppap_date": "_",
            "sop_year": "2028",
            "rfq_reception_date": "2026-12-01",
            "quotation_expected_date": "2026-12-15",
            "contact_name": "Jane Doe",
            "contact_role": "Buyer",
            "contact_phone": "+33 1 23 45 67 89",
        },
        force_internal_contact_explanation=True,
    )

    assert "internal Avocarbon address" in text
    assert text.endswith("What is the Contact email?")


def test_tool_messages_indicate_internal_contact_blocked():
    assert (
        chat._tool_messages_indicate_internal_contact_blocked(
            [
                {
                    "name": "updateFormFields",
                    "content": json.dumps(
                        {
                            "success": False,
                            "status": "internal_contact_blocked",
                            "blocked_internal_contact_fields": ["contact_email"],
                        }
                    ),
                }
            ]
        )
        is True
    )


def test_extract_successful_submit_validation_payload_returns_success_payload():
    payload = chat._extract_successful_submit_validation_payload(
        [
            {
                "name": "submitValidation",
                "content": json.dumps(
                    {
                        "success": True,
                        "sub_status": "PENDING_FOR_VALIDATION",
                    }
                ),
            }
        ]
    )

    assert payload == {
        "success": True,
        "sub_status": "PENDING_FOR_VALIDATION",
    }


def test_build_submit_validation_success_text_uses_document_type():
    rfq = SimpleNamespace(document_type=chat.RfqDocumentType.RFI)

    assert chat._build_submit_validation_success_text(
        rfq,
        {"sub_status": "PENDING_FOR_VALIDATION"},
    ) == (
        "Your RFI was submitted and is now PENDING_FOR_VALIDATION. "
        "The validation workflow has started."
    )


def test_sanitize_chat_history_reuses_assistant_sanitizer_for_persisted_messages():
    history = [
        {
            "role": "assistant",
            "content": (
                "Saved.\n\n"
                '{"fields_to_update": {"customer_name": "Nidec"}}\n\n'
                "Please continue."
            ),
        }
    ]

    assert chat._sanitize_chat_history(history) == [
        {
            "role": "assistant",
            "content": "Saved.\n\nPlease continue.",
        }
    ]


def test_is_field_filled_keeps_optional_skip_but_rejects_required_skip():
    assert chat._is_field_filled({"ppap_date": "_"}, "ppap_date") is True
    assert chat._is_field_filled({"po_date": "_"}, "po_date") is False
    assert chat._is_field_filled({"scope": "skip"}, "scope") is False


def test_is_field_filled_treats_internal_avocarbon_contact_as_missing():
    polluted_contact = {
        "contact_email": "ons.ghariani@avocarbon.com",
        "contact_name": "Ons Ghariani",
        "contact_role": "Sales",
        "contact_phone": "+216 00 000 000",
    }

    assert chat._is_field_filled(polluted_contact, "contact_email") is False
    assert chat._is_field_filled(polluted_contact, "contact_name") is False
    assert chat._is_field_filled(polluted_contact, "contact_role") is False
    assert chat._is_field_filled(polluted_contact, "contact_phone") is False


def test_get_current_step_includes_optional_rfq_fields_in_order():
    data = {
        "customer_name": "TPEG",
        "application": "Electronic",
        "product_name": "Rod Choke",
        "product_line_acronym": "ROC",
        "project_name": "TPEG Winding",
        "costing_data": "",
        "rfq_files": ["TP018157Arev1_Draft.pdf"],
        "products": [
            {
                "part_number": "TP018157A",
                "revision_level": "",
                "quantity": 1000,
                "target_price": 7,
                "currency": "EUR",
                "target_price_is_estimated": True,
            }
        ],
        "delivery_zone": "Europe",
        "delivery_plant": "Tunisia",
        "country": "France",
        "po_date": "2027-12-12",
        "ppap_date": "",
        "sop_year": 2027,
        "rfq_reception_date": "2027-05-05",
        "quotation_expected_date": "2027-04-14",
        "contact_name": "Khouloud Aouini",
        "contact_role": "Method & Industrialization Engineer",
        "contact_phone": "+216 98 148 178",
        "contact_email": "khouloud.aouini@tpe.group",
    }

    current_step, missing_fields = chat._get_current_step_and_missing_fields(
        "rfq",
        data,
    )

    assert current_step == 1
    assert missing_fields == ["ppap_date"]


def test_system_prompt_allows_grouped_product_rows_to_omit_revision_level():
    assert (
        "When asking for a full product row in one grouped prompt, do NOT tell "
        "the user to type `skip` for Revision Level; they may simply leave it out."
        in chat.SYSTEM_PROMPT
    )
    assert (
        "If it is omitted while the required product row values are present, treat "
        "Revision Level as blank, save the row, and continue without a dedicated "
        "follow-up question."
        in chat.SYSTEM_PROMPT
    )


def test_incomplete_product_fields_ignore_missing_optional_revision_level():
    assert chat.get_incomplete_product_fields(
        {
            "products": [
                {
                    "part_number": "P58654",
                    "revision_level": "",
                    "quantity": 1000,
                    "target_price": 100,
                    "currency": "INR",
                    "target_price_is_estimated": True,
                }
            ]
        },
        include_optional=True,
    ) == []


def test_sanitize_rfq_update_fields_rejects_required_skips_and_keeps_optional_skips():
    (
        sanitized_fields,
        rejected_required_fields,
        blocked_internal_contact_fields,
    ) = (
        chat._sanitize_rfq_update_fields_for_chat(
            {
                "po_date": "skip",
                "ppap_date": "skip",
                "products": [
                    {
                        "part_number": "skip",
                        "revision_level": "skip",
                        "quantity": 1000,
                        "target_price": 1,
                        "currency": "skip",
                        "target_price_is_estimated": True,
                    }
                ],
            }
        )
    )

    assert "po_date" not in sanitized_fields
    assert sanitized_fields["ppap_date"] == "_"
    assert sanitized_fields["products"][0]["part_number"] is None
    assert sanitized_fields["products"][0]["revision_level"] == ""
    assert sanitized_fields["products"][0]["currency"] is None
    assert set(rejected_required_fields) == {
        "po_date",
        "products[0].part_number",
        "products[0].currency",
    }
    assert blocked_internal_contact_fields == []


def test_sanitize_rfq_update_fields_blocks_internal_avocarbon_contact_fields():
    (
        sanitized_fields,
        rejected_required_fields,
        blocked_internal_contact_fields,
    ) = (
        chat._sanitize_rfq_update_fields_for_chat(
            {
                "customer_name": "Bosch",
                "contact_email": "ons.ghariani@avocarbon.com",
                "contact_name": "Ons Ghariani",
                "contact_role": "Sales Engineer",
                "contact_phone": "+216 11 222 333",
            }
        )
    )

    assert sanitized_fields == {"customer_name": "Bosch"}
    assert rejected_required_fields == []
    assert set(blocked_internal_contact_fields) == {
        "contact_email",
        "contact_name",
        "contact_role",
        "contact_phone",
    }


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_fx_payload(monkeypatch):
    fx_db = object()

    async def _fake_get_rate(currency_code, db3):
        assert currency_code == "USD"
        assert db3 is fx_db
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
        db3=fx_db,
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
    fx_db = object()

    async def _fake_get_rate(currency_code, db3):
        assert currency_code == "MXN"
        assert db3 is fx_db
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
        db3=fx_db,
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

    async def flush(self):
        return None


class _UnexpectedHttpClient:
    async def get(self, *args, **kwargs):
        raise AssertionError("HTTP client should not be called in this scenario.")


class _StaticTextHttpClient:
    def __init__(self, text):
        self._text = text

    async def get(self, *args, **kwargs):
        return SimpleNamespace(text=self._text)


def _build_matrix():
    return SimpleNamespace(
        product_line="Brushes",
        acronym="BRU",
        n3_kam_limit=250,
        n2_zone_limit=750,
        n1_vp_limit=1500,
    )


def _build_rfq(**overrides):
    data = {
        "created_by_email": "owner@example.com",
        "document_type": chat.RfqDocumentType.RFQ,
        "sub_status": chat.RfqSubStatus.NEW_RFQ,
        "product_line_acronym": None,
        "zone_manager_email": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _build_product(**overrides):
    data = {
        "part_number": "PN-001",
        "revision_level": "A",
        "quantity": 500000,
        "target_price": 1.25,
        "currency": "EUR",
        "target_price_is_estimated": True,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_check_contact_existence_blocks_internal_avocarbon_email_without_http_call():
    extracted_data = {}

    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "contact-internal-1",
                "name": "checkContactExistence",
                "arguments": {
                    "contact_email": "ons.ghariani@avocarbon.com",
                },
            }
        ],
        http_client=_UnexpectedHttpClient(),
        db=None,
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["exists"] is False
    assert payload["internal_contact"] is True
    assert "customer contacts" in payload["message"]
    assert "contact_email" not in extracted_data


@pytest.mark.asyncio
async def test_check_group_existence_normalizes_non_json_tool_response():
    extracted_data = {}

    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "group-invalid-json-1",
                "name": "checkGroupeExistence",
                "arguments": {
                    "groupeName": "Valeo India",
                },
            }
        ],
        http_client=_StaticTextHttpClient(
            "Failed to parse as JSON: unexpected character: line 1 column 1 (char 0)"
        ),
        db=None,
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["exists"] is False
    assert payload["matches"] == []
    assert payload["tool_error"] == "invalid_json_response"
    assert "Failed to parse as JSON" not in tool_messages[0]["content"]
    assert extracted_data["customer_name"] == "Valeo India"


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_zone_manager_payload_with_canonical_zone():
    extracted_data = {
        "products": [_build_product()],
        "to_total": "10",
    }
    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-1",
                "name": "retrieveZoneManager",
                "arguments": {
                    "product_line_acronym": "BRU",
                    "delivery_zone": "North America",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["validator_role"] == "Zone Manager"
    assert payload["zone_manager_email"] == "dean.hayward@avocarbon.com"
    assert payload["delivery_zone"] == "North America"
    assert payload["to_total"] == 625.0
    assert extracted_data["delivery_zone"] == "North America"
    assert extracted_data["to_total"] == "625.0"


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_error_for_unknown_zone_manager_zone():
    extracted_data = {
        "products": [_build_product(quantity=400000)],
    }
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-2",
                "name": "retrieveZoneManager",
                "arguments": {
                    "product_line_acronym": "BRU",
                    "delivery_zone": "antarctica",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert "error" in payload
    assert payload["approved_delivery_zones"] == [
        "Europe",
        "Africa",
        "India",
        "North America",
        "South America",
        "China / South Pacific",
        "Korea / Japan",
    ]
    assert payload["to_total"] == 500.0
    assert extracted_data["to_total"] == "500.0"


@pytest.mark.asyncio
async def test_execute_tool_calls_returns_error_when_turnover_inputs_are_missing():
    extracted_data = {
        "products": [
            {
                "part_number": "PN-001",
                "revision_level": "A",
                "target_price": 1.25,
                "currency": "EUR",
                "target_price_is_estimated": True,
            }
        ],
    }
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-3",
                "name": "retrieveZoneManager",
                "arguments": {
                    "product_line_acronym": "BRU",
                    "delivery_zone": "Europe",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["error"] == "Complete products before validator routing: products[1].quantity"


@pytest.mark.asyncio
async def test_update_form_fields_preserves_product_row_price_source_as_boolean():
    extracted_data = {}
    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "update-flag-1",
                "name": "updateFormFields",
                "arguments": {
                    "fields_to_update": {
                        "products": [
                            {
                                "part_number": "PN-9",
                                "revision_level": "C",
                                "quantity": 10,
                                "target_price": 1.25,
                                "currency": "EUR",
                                "target_price_is_estimated": "yes",
                            }
                        ],
                    }
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["success"] is True
    assert extracted_data["products"][0]["target_price_is_estimated"] is True
    assert isinstance(extracted_data["products"][0]["target_price_is_estimated"], bool)
    assert "target_price_is_estimated" not in extracted_data


@pytest.mark.asyncio
async def test_update_form_fields_blocks_internal_avocarbon_contact_fields():
    extracted_data = {}

    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "update-internal-contact-1",
                "name": "updateFormFields",
                "arguments": {
                    "fields_to_update": {
                        "contact_email": "ons.ghariani@avocarbon.com",
                        "contact_name": "Ons Ghariani",
                        "contact_role": "Sales Engineer",
                        "contact_phone": "+216 11 222 333",
                    }
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["success"] is False
    assert payload["status"] == "internal_contact_blocked"
    assert set(payload["blocked_internal_contact_fields"]) == {
        "contact_email",
        "contact_name",
        "contact_role",
        "contact_phone",
    }
    assert extracted_data == {}


def test_normalize_tool_arguments_preserves_append_products_flag():
    normalized = chat._normalize_tool_arguments(
        "updateFormFields",
        {
            "appendProducts": "true",
            "fields_to_update": {"products": []},
        },
    )

    assert normalized["append_products"] is True


def test_normalize_tool_arguments_preserves_false_append_products_flag():
    normalized = chat._normalize_tool_arguments(
        "updateFormFields",
        {
            "append_products": False,
            "fields_to_update": {"products": []},
        },
    )

    assert normalized["append_products"] is False


def test_update_form_fields_tool_schema_exposes_append_products():
    update_form_fields_tool = next(
        tool["function"]
        for tool in chat.TOOLS
        if tool["function"]["name"] == "updateFormFields"
    )
    properties = update_form_fields_tool["parameters"]["properties"]

    assert "append_products" in properties
    assert properties["append_products"]["type"] == "boolean"
    assert (
        properties["append_products"]["description"]
        == "Set this to true when adding additional part numbers/products to an existing list. If false, it will overwrite the entire product list."
    )


@pytest.mark.asyncio
async def test_update_form_fields_initial_product_save_without_append_creates_products():
    extracted_data = {}
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "append-products-0",
                "name": "updateFormFields",
                "arguments": {
                    "fields_to_update": {
                        "products": [
                            {
                                "part_number": "PN-1",
                                "revision_level": "A",
                                "quantity": 100,
                                "target_price": 2.5,
                                "currency": "EUR",
                                "target_price_is_estimated": True,
                            }
                        ]
                    },
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["success"] is True
    assert [product["part_number"] for product in extracted_data["products"]] == ["PN-1"]


@pytest.mark.asyncio
async def test_update_form_fields_appends_products_sequentially():
    extracted_data = {
        "products": [
            _build_product(part_number="PN-1", quantity=100, target_price=2.5)
        ]
    }
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "append-products-1",
                "name": "updateFormFields",
                "arguments": {
                    "append_products": True,
                    "fields_to_update": {
                        "products": [
                            {
                                "part_number": "PN-2",
                                "revision_level": "B",
                                "quantity": 200,
                                "target_price": 3.0,
                                "currency": "EUR",
                                "target_price_is_estimated": False,
                            }
                        ]
                    },
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["success"] is True
    assert [product["part_number"] for product in extracted_data["products"]] == [
        "PN-1",
        "PN-2",
    ]
    assert extracted_data["products"][1]["currency"] == "EUR"
    assert extracted_data["products"][1]["target_price_is_estimated"] is False


@pytest.mark.asyncio
async def test_update_form_fields_appends_products_from_persisted_rfq_state():
    extracted_data = {}
    rfq = _build_rfq(
        rfq_data={
            "products": [
                _build_product(part_number="PN-1", quantity=100, target_price=2.5)
            ]
        }
    )
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "append-products-persisted",
                "name": "updateFormFields",
                "arguments": {
                    "append_products": True,
                    "fields_to_update": {
                        "products": [
                            {
                                "part_number": "PN-2",
                                "revision_level": "B",
                                "quantity": 200,
                                "target_price": 3.0,
                                "currency": "EUR",
                                "target_price_is_estimated": False,
                            }
                        ]
                    },
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=rfq,
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["success"] is True
    assert [product["part_number"] for product in extracted_data["products"]] == [
        "PN-1",
        "PN-2",
    ]


@pytest.mark.asyncio
async def test_update_form_fields_without_append_overwrites_products():
    extracted_data = {
        "products": [
            _build_product(part_number="PN-1", quantity=100, target_price=2.5)
        ]
    }
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "overwrite-products-1",
                "name": "updateFormFields",
                "arguments": {
                    "fields_to_update": {
                        "products": [
                            {
                                "part_number": "PN-9",
                                "revision_level": "Z",
                                "quantity": 900,
                                "target_price": 9.0,
                                "currency": "EUR",
                                "target_price_is_estimated": False,
                            }
                        ]
                    },
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["success"] is True
    assert [product["part_number"] for product in extracted_data["products"]] == ["PN-9"]


@pytest.mark.asyncio
async def test_update_form_fields_rejects_mixed_product_currencies():
    extracted_data = {
        "products": [
            _build_product(part_number="PN-1", quantity=100, target_price=2.5)
        ]
    }
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "append-products-2",
                "name": "updateFormFields",
                "arguments": {
                    "append_products": True,
                    "fields_to_update": {
                        "products": [
                            {
                                "part_number": "PN-2",
                                "revision_level": "B",
                                "quantity": 200,
                                "target_price": 3.0,
                                "currency": "USD",
                                "target_price_is_estimated": False,
                            }
                        ]
                    },
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["success"] is False
    assert "same currency" in payload["error"]
    assert len(extracted_data["products"]) == 1


@pytest.mark.asyncio
async def test_update_form_fields_maps_product_name_to_product_line_acronym():
    extracted_data = {}
    tool_messages, _ = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "map-product-line-1",
                "name": "updateFormFields",
                "arguments": {
                    "fields_to_update": {
                        "product_name": "brushes",
                    }
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert payload["success"] is True
    assert extracted_data["product_name"] == "Brushes"
    assert extracted_data["product_line_acronym"] == "BRU"


@pytest.mark.asyncio
async def test_retrieve_zone_manager_uses_saved_product_line_when_tool_arg_missing():
    extracted_data = {
        "products": [_build_product()],
    }
    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-4",
                "name": "retrieveZoneManager",
                "arguments": {
                    "delivery_zone": "Europe",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=None,
        rfq=_build_rfq(product_line_acronym="BRU"),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["validator_role"] == "Zone Manager"
    assert payload["delivery_zone"] == "Europe"
    assert payload["zone_manager_email"] == "franck.lagadec@avocarbon.com"


@pytest.mark.asyncio
async def test_execute_tool_calls_uses_fx_for_non_eur_routing_without_mutating_products(monkeypatch):
    fx_db = object()

    async def _fake_get_rate(currency_code, db3):
        assert currency_code == "INR"
        assert db3 is fx_db
        return 0.01

    monkeypatch.setattr(chat, "get_eur_exchange_rate", _fake_get_rate)

    extracted_data = {
        "products": [
            _build_product(
                quantity=10,
                target_price=2000,
                currency="INR",
                target_price_is_estimated=False,
            )
        ],
        "total_target_to": 20000,
        "target_price_currency": "INR",
    }
    tool_messages, auto_redirect = await chat._execute_tool_calls(
        tool_calls=[
            {
                "id": "zone-5",
                "name": "retrieveZoneManager",
                "arguments": {
                    "product_line_acronym": "BRU",
                    "delivery_zone": "Europe",
                },
            }
        ],
        http_client=None,
        db=_FakeDb(_build_matrix()),
        db3=fx_db,
        rfq=_build_rfq(),
        current_user=SimpleNamespace(email="user@example.com"),
        extracted_data=extracted_data,
        chat_mode="rfq",
        tool_calls_used=[],
    )

    payload = json.loads(tool_messages[0]["content"])

    assert auto_redirect is False
    assert payload["products"][0]["target_price"] == 2000
    assert payload["products"][0]["currency"] == "INR"
    assert payload["total_target_to"] == 20000
    assert payload["to_total"] == 0.2
    assert payload["to_total_local"] == "20.0"


def test_system_prompt_includes_dimension_fx_and_delivery_zone_instructions():
    assert "Always store dimension data in mm." in chat.SYSTEM_PROMPT
    assert "strictly forbidden from calculating exchange rates or converting currencies yourself" in chat.SYSTEM_PROMPT
    assert 'If the user says "2000 INR", you must save `target_price = 2000` and `currency = "INR"`' in chat.SYSTEM_PROMPT
    assert "CRITICAL NO-ROUNDING RULE" in chat.SYSTEM_PROMPT
    assert "Keep at most 5 digits after the decimal point." in chat.SYSTEM_PROMPT
    assert "save 0.19879 into the database" in chat.SYSTEM_PROMPT
    assert "Target Price" in chat.SYSTEM_PROMPT
    assert "TARGET PRICE FORMAT RULE" in chat.SYSTEM_PROMPT
    assert "Price source (Must be either 'Estimated' or 'Official Customer Price')" in chat.SYSTEM_PROMPT
    assert "FORBIDDEN from flattening" in chat.SYSTEM_PROMPT
    assert "CRITICAL OUTPUT RULES" in chat.SYSTEM_PROMPT
    assert "NO SCRATCHPAD MATH" in chat.SYSTEM_PROMPT
    assert "NO GUESSING/PROPOSITIONS" in chat.SYSTEM_PROMPT
    assert "ENUM EXCEPTION" in chat.SYSTEM_PROMPT
    assert "FINAL CONFIRMATION RULE" in chat.SYSTEM_PROMPT
    assert "NEVER write '1. Yes'" in chat.SYSTEM_PROMPT
    assert "strict boolean choices" in chat.SYSTEM_PROMPT
    assert "target_price_is_estimated" in chat.SYSTEM_PROMPT
    assert "MUST call `get_eur_exchange_rate`" in chat.SYSTEM_PROMPT
    assert "THIS IS A CONVERSATIONAL COMMAND, NOT DATA" in chat.SYSTEM_PROMPT
    assert "strictly forbidden from calling the updateFormFields tool to save '1' or '2' into any RFQ field" in chat.SYSTEM_PROMPT
    assert "ONLY when the user provides explicit, contextual business data intended for the RFQ form" in chat.SYSTEM_PROMPT
    assert "Menu choices, guidance-mode selections, language selections, and other conversational control commands are NOT RFQ data" in chat.SYSTEM_PROMPT
    assert "truncate it instead of rounding" in chat.SYSTEM_PROMPT
    assert "Ask the user to restate the Target Price directly in EUR" in chat.SYSTEM_PROMPT
    assert "You MUST NOT rewrite `products[*].target_price`" in chat.SYSTEM_PROMPT
    assert "MUST NEVER calculate the TO Total yourself" in chat.SYSTEM_PROMPT
    assert "return the calculated `to_total` to you" in chat.SYSTEM_PROMPT
    assert "exactly one of these 7 approved `delivery_zone` strings" in chat.SYSTEM_PROMPT
    assert "France -> Europe, South Africa -> Africa, India -> India, United States -> North America, Brazil -> South America, China -> China / South Pacific, Japan -> Korea / Japan" in chat.SYSTEM_PROMPT
    assert "Any `delivery_zone` you send through `updateFormFields` MUST exactly match one of the 7 approved strings" in chat.SYSTEM_PROMPT
    assert "you MUST present only these exact 7 options and no others" in chat.SYSTEM_PROMPT
    assert "Would you like to add another part number to this request?" in chat.SYSTEM_PROMPT
    assert "NEVER ask the user how many part numbers/products there are upfront." in chat.SYSTEM_PROMPT
    assert "NEVER ask the user for the Product Line acronym." in chat.SYSTEM_PROMPT
    assert "Any email address from the `avocarbon.com` domain is an internal Avocarbon address, not a customer contact." in chat.SYSTEM_PROMPT
    assert "an `@avocarbon.com` email does NOT count as a valid customer contact email" in chat.SYSTEM_PROMPT
    assert "you MUST still ask optional RFQ fields when they appear next in the checklist order" in chat.SYSTEM_PROMPT
    assert "When you ask any other optional field, you MUST clearly say it is optional and that the user can type `skip` to leave it blank." in chat.SYSTEM_PROMPT
    assert "Type of Packaging (OPTIONAL — allow `skip`)" in chat.SYSTEM_PROMPT
    assert "append_products=true" in chat.SYSTEM_PROMPT
    assert 'When the user agrees to add a second, third, or subsequent part number, you MUST call updateFormFields with the argument "append_products": true.' in chat.SYSTEM_PROMPT
    assert "Request-level pricing metadata if still missing" not in chat.SYSTEM_PROMPT
    assert "MUST NOT jump to validator routing or ask for submission" in chat.SYSTEM_PROMPT
    assert "save both `product_name` and the authorized `product_line_acronym`" not in chat.SYSTEM_PROMPT
    assert "When the user confirms submission, you MUST ONLY invoke the submitValidation tool." in chat.SYSTEM_PROMPT
    assert "Do NOT output any standard text, do NOT explain your reasoning, and do NOT narrate that you are calling the tool." in chat.SYSTEM_PROMPT
    assert "Acknowledge the submission and confirm it is PENDING_FOR_VALIDATION." not in chat.SYSTEM_PROMPT


def test_product_item_tool_schema_preserves_raw_currency_fields():
    properties = chat.PRODUCT_ITEM_TOOL_SCHEMA["items"]["properties"]

    assert "currency" in properties
    assert "target_price_is_estimated" in properties
    assert "Never convert currencies yourself" in properties["target_price"]["description"]
    assert "Derived turnover only" in properties["target_to"]["description"]


def test_dynamic_prompt_reinforces_delivery_zone_sync_rules():
    source = inspect.getsource(chat.handle_chat)

    assert "frontend form stays synchronized with the latest data" in source
    assert "approved values before calling `updateFormFields`: `Europe`, `Africa`, `India`, `North America`, `South America`, `China / South Pacific`, `Korea / Japan`" in source
    assert 'If the user replies with "1" or "2" to choose between step-by-step guidance and paragraph mode, treat that reply as a conversational command only.' in source
    assert "You MUST NOT call `updateFormFields` for it or save it into any RFQ field." in source
