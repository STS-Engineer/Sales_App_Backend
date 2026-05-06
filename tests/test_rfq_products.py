from app.schemas.rfq import (
    get_incomplete_product_fields,
    normalize_rfq_data_products,
    rfq_data_payload_to_dict,
)


def test_normalize_multi_product_rows_computes_totals_and_legacy_mirrors():
    data = normalize_rfq_data_products(
        {
            "products": [
                {
                    "part_number": "PN-1",
                    "revision_level": "A",
                    "quantity": "1000",
                    "target_price": "2.5",
                },
                {
                    "partNumber": "PN-2",
                    "revisionLevel": "B",
                    "qty": "2000",
                    "targetPrice": "3",
                },
            ]
        },
        products_authoritative=True,
    )

    assert data["products"][0]["target_to"] == 2500
    assert data["products"][1]["target_to"] == 6000
    assert data["total_target_to"] == 8500
    assert data["to_total"] == 8.5
    assert data["customer_pn"] == "PN-1"
    assert data["revision_level"] == "A"
    assert data["annual_volume"] == 1000
    assert data["target_price_eur"] == 2.5


def test_legacy_single_product_fields_are_exposed_as_products():
    data = normalize_rfq_data_products(
        {
            "customer_pn": "LEGACY-PN",
            "revision_level": "00",
            "annual_volume": "500,000",
            "target_price_eur": "0.25",
        }
    )

    assert data["products"] == [
        {
            "part_number": "LEGACY-PN",
            "revision_level": "00",
            "quantity": 500000,
            "target_price": 0.25,
            "target_to": 125000,
        }
    ]
    assert data["total_target_to"] == 125000
    assert data["to_total"] == 125


def test_partial_product_rows_are_allowed_until_submission():
    data = rfq_data_payload_to_dict(
        {
            "products": [
                {
                    "part_number": "DRAFT-PN",
                    "revision_level": "",
                    "quantity": None,
                    "target_price": 1.2,
                }
            ]
        }
    )

    assert data["products"][0]["part_number"] == "DRAFT-PN"
    assert get_incomplete_product_fields(data) == [
        "products[1].revision_level",
        "products[1].quantity",
    ]

