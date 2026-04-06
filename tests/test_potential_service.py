from app.models.potential import Potential
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
