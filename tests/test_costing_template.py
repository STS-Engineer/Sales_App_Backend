from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.services.costing_template import render_costing_template_html


def test_render_costing_template_html_includes_all_rfq_step_fields():
    rfq = Rfq(
        rfq_id="rfq-123",
        created_by_email="sales@example.com",
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        rfq_data={
            "systematic_rfq_id": "RFQ-001",
            "customer_name": "ACME",
            "project_name": "Brake Booster",
            "type_of_packaging": "Returnable packaging",
        },
    )

    html = render_costing_template_html(rfq)

    assert "Project name" in html
    assert "Brake Booster" in html
    assert "Type of packaging" in html
    assert "Returnable packaging" in html


def test_render_costing_template_html_lists_all_product_references():
    rfq = Rfq(
        rfq_id="rfq-456",
        created_by_email="sales@example.com",
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        rfq_data={
            "systematic_rfq_id": "RFQ-002",
            "customer_name": "ACME",
            "products": [
                {
                    "part_number": "PN-001",
                    "revision_level": "A1",
                    "quantity": 120000,
                    "target_price": 1.75,
                    "currency": "EUR",
                    "target_price_is_estimated": False,
                    "target_to": 210000,
                },
                {
                    "part_number": "PN-002",
                    "revision_level": "B4",
                    "quantity": 80000,
                    "target_price": 2.4,
                    "currency": "EUR",
                    "target_price_is_estimated": True,
                    "target_to": 192000,
                },
            ],
        },
    )

    html = render_costing_template_html(rfq)

    assert "Customer details" in html
    assert "Products" in html
    assert "PN-001" in html
    assert "PN-002" in html
    assert "A1" in html
    assert "B4" in html
    assert "Official customer price" in html
    assert "Estimated" in html


def test_render_costing_template_html_shows_local_and_eur_amounts():
    rfq = Rfq(
        rfq_id="rfq-789",
        created_by_email="sales@example.com",
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        rfq_data={
            "systematic_rfq_id": "RFQ-003",
            "customer_name": "ACME",
            "target_price_local": 10,
            "target_price_currency": "USD",
            "target_price_eur": 9,
            "total_target_to": 500000,
            "to_total": 450,
            "to_total_local": 500,
            "products": [
                {
                    "part_number": "PN-USD",
                    "revision_level": "C2",
                    "quantity": 50000,
                    "target_price": 10,
                    "currency": "USD",
                    "target_price_is_estimated": False,
                    "target_to": 500000,
                }
            ],
        },
    )

    html = render_costing_template_html(rfq)

    assert "10 USD / 9 EUR" in html
    assert "500 kUSD / 450 kEUR" in html
