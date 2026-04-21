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
