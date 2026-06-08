import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/rfq_test",
)
os.environ.setdefault(
    "DATABASE_URL3",
    "postgresql://user:password@localhost:5432/rfq_fx_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.routers.chat import (
    RFQ_PARAGRAPH_MODE_PROMPT,
    _build_missing_fields_prompt,
    _get_current_step_and_missing_fields,
    _history_uses_paragraph_mode,
)


def _build_base_rfq_state():
    return {
        "customer_name": "Nidec",
        "application": "Traction motor",
        "product_name": "Brush Holder",
        "product_line_acronym": "BRU",
        "project_name": "Project Alpha",
        "costing_data": "Not provided",
        "rfq_files": [{"name": "drawing.pdf"}],
        "products": [
            {
                "part_number": "PN-001",
                "revision_level": "01",
                "quantity": 500000,
                "target_price": 1.25,
                "currency": "EUR",
                "target_price_is_estimated": True,
            }
        ],
        "delivery_zone": "Europe",
        "delivery_plant": "Plant A",
        "country": "France",
        "po_date": "2026-04-20",
        "ppap_date": "2026-05-20",
        "sop_year": "2027",
        "rfq_reception_date": "2026-04-10",
        "quotation_expected_date": "2026-04-30",
        "contact_email": "buyer@example.com",
        "contact_name": "Jane Doe",
        "contact_role": "Buyer",
        "contact_phone": "+33 1 23 45 67 89",
        "expected_delivery_conditions": "DAP",
        "expected_payment_terms": "60 days",
        "type_of_packaging": "returnable plastic tray",
        "business_trigger": "Cost reduction",
        "customer_tooling_conditions": "Customer owned",
        "entry_barriers": "Qualification lead time",
        "responsibility_design": "Design team",
        "responsibility_validation": "Validation team",
        "product_ownership": "Avocarbon",
        "pays_for_development": "Customer",
        "capacity_available": "Yes",
        "scope": "In scope",
    }


def test_rfq_step_3_includes_strategic_note_and_final_recommendation():
    rfq_state = _build_base_rfq_state()

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 3
    assert missing_fields == ["strategic_note", "final_recommendation"]

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "Strategic note" in prompt
    assert "Final recommendation" in prompt
    assert "customer_status" not in prompt
    assert "Missing fields you must ASK THE USER for" in prompt


def test_required_step_gating_stops_at_first_incomplete_step():
    rfq_state = _build_base_rfq_state()
    rfq_state.pop("rfq_files", None)
    rfq_state.pop("type_of_packaging", None)

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 1
    assert missing_fields == ["rfq_files"]

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "Next field to ask for: RFQ Files" in prompt
    assert "Type of Packaging (OPTIONAL)" not in prompt


def test_paragraph_mode_keeps_earlier_step_1_fields_before_rfq_files():
    rfq_state = _build_base_rfq_state()
    rfq_state.pop("rfq_files", None)
    rfq_state.pop("product_name", None)
    rfq_state.pop("product_line_acronym", None)

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 1
    assert missing_fields == ["product_name", "product_line_acronym", "rfq_files"]

    prompt = _build_missing_fields_prompt(
        "rfq",
        rfq_state,
        prioritize_rfq_files=True,
    )

    assert "Next field to ask for: Product name" in prompt
    assert "PARAGRAPH MODE FILE BLOCKER" not in prompt


def test_paragraph_mode_prioritizes_rfq_files_before_later_step_1_fields():
    rfq_state = _build_base_rfq_state()
    rfq_state.pop("rfq_files", None)
    rfq_state.pop("contact_phone", None)
    rfq_state.pop("contact_email", None)

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 1
    assert missing_fields == ["rfq_files", "contact_phone", "contact_email"]

    prompt = _build_missing_fields_prompt(
        "rfq",
        rfq_state,
        prioritize_rfq_files=True,
    )

    assert "Next field to ask for: RFQ Files" in prompt
    assert "PARAGRAPH MODE FILE BLOCKER" in prompt


def test_history_uses_paragraph_mode_detects_paragraph_prompt():
    history = [
        {"role": "assistant", "content": RFQ_PARAGRAPH_MODE_PROMPT},
        {"role": "user", "content": "My RFQ paragraph"},
    ]

    assert _history_uses_paragraph_mode(history) is True


def test_step_1_optional_fields_do_not_block_progression():
    rfq_state = _build_base_rfq_state()
    rfq_state.pop("costing_data", None)
    rfq_state.pop("ppap_date", None)
    rfq_state["products"] = [
        {
            "part_number": "PN-001",
            "revision_level": "",
            "quantity": 500000,
            "target_price": 1.25,
            "currency": "EUR",
            "target_price_is_estimated": True,
        }
    ]

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 3
    assert missing_fields == ["strategic_note", "final_recommendation"]

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "Costing data (OPTIONAL)" not in prompt
    assert "PPAP date (OPTIONAL)" not in prompt
    assert "Product 1 Revision level (OPTIONAL)" not in prompt


def test_step_2_optional_fields_do_not_block_progression():
    rfq_state = _build_base_rfq_state()
    rfq_state.pop("type_of_packaging", None)
    rfq_state.pop("business_trigger", None)
    rfq_state.pop("customer_tooling_conditions", None)
    rfq_state.pop("entry_barriers", None)

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 3
    assert missing_fields == ["strategic_note", "final_recommendation"]

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "Type of Packaging (OPTIONAL)" not in prompt
    assert "Business Trigger (OPTIONAL)" not in prompt


def test_product_revision_level_does_not_block_progression():
    rfq_state = _build_base_rfq_state()
    rfq_state["products"] = [
        {
            "part_number": "PN-001",
            "revision_level": "",
            "quantity": 500000,
            "target_price": 1.25,
            "currency": "EUR",
            "target_price_is_estimated": True,
        }
    ]

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 3
    assert missing_fields == ["strategic_note", "final_recommendation"]

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "Product 1 Revision level (OPTIONAL)" not in prompt


def test_missing_fields_prompt_uses_plain_labels_without_examples():
    rfq_state = _build_base_rfq_state()
    rfq_state.pop("expected_delivery_conditions", None)

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 2
    assert missing_fields[0] == "expected_delivery_conditions"

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "Expected Delivery Conditions" in prompt
    assert "e.g." not in prompt
    assert "for example" not in prompt.casefold()
