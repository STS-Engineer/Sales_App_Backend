import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/rfq_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.routers.chat import _build_missing_fields_prompt, _get_current_step_and_missing_fields


def test_rfq_step_3_includes_strategic_note_and_final_recommendation():
    rfq_state = {
        "customer_name": "Nidec",
        "application": "Traction motor",
        "product_name": "Brush Holder",
        "product_line_acronym": "BRU",
        "project_name": "Project Alpha",
        "rfq_files": [{"name": "drawing.pdf"}],
        "customer_pn": "PN-001",
        "revision_level": "01",
        "delivery_zone": "Europe",
        "delivery_plant": "Plant A",
        "country": "France",
        "po_date": "2026-04-20",
        "ppap_date": "2026-05-20",
        "sop_year": "2027",
        "annual_volume": "500000",
        "rfq_reception_date": "2026-04-10",
        "quotation_expected_date": "2026-04-30",
        "contact_email": "buyer@example.com",
        "contact_name": "Jane Doe",
        "contact_role": "Buyer",
        "contact_phone": "+33 1 23 45 67 89",
        "target_price_eur": "1.25",
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
        "customer_status": "Existing customer",
    }

    current_step, missing_fields = _get_current_step_and_missing_fields("rfq", rfq_state)

    assert current_step == 3
    assert missing_fields == ["strategic_note", "final_recommendation"]

    prompt = _build_missing_fields_prompt("rfq", rfq_state)

    assert "strategic_note" in prompt
    assert "final_recommendation" in prompt
    assert "Missing fields you must ASK THE USER for" in prompt
