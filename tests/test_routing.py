"""
Test module for the routing (escalation) service.

These are pure unit tests -- no DB required.
"""
def test_calculate_pte():
    from app.services.routing import calculate_pte

    # 10 EUR * 50_000 = 500_000 -> 500 KEUR
    assert calculate_pte(10.0, 50_000) == 500.0

    # 0.5 EUR * 100 = 50 -> 0.05 KEUR
    assert abs(calculate_pte(0.5, 100) - 0.05) < 1e-9

    # 200 EUR * 1000 = 200_000 -> 200 KEUR
    assert calculate_pte(200.0, 1000) == 200.0


def test_escalation_constants():
    from app.services.routing import (
        N2_ASIA_EAST_EMAIL,
        N2_ASIA_SOUTH_EMAIL,
        N2_ZONE_EMAIL,
        N2_AMERICAS_EMAIL,
        N1_VP_EMAIL,
        N0_CEO_EMAIL,
    )

    assert N2_ASIA_EAST_EMAIL == "tao.ren@avocarbon.com"
    assert N2_ASIA_SOUTH_EMAIL == "eipe.thomas@avocarbon.com"
    assert N2_ZONE_EMAIL == "franck.lagadec@avocarbon.com"
    assert N2_AMERICAS_EMAIL == "dean.hayward@avocarbon.com"
    assert N1_VP_EMAIL == "eric.suszylo@avocarbon.com"
    assert N0_CEO_EMAIL == "taha.khiari@avocarbon.com"


def test_normalize_delivery_zone_handles_new_zone_variants():
    from app.services.routing import normalize_delivery_zone

    assert normalize_delivery_zone("Europe") == "Europe"
    assert normalize_delivery_zone("north-america") == "North America"
    assert normalize_delivery_zone("South America") == "South America"
    assert normalize_delivery_zone("china/south pacific") == "China / South Pacific"
    assert normalize_delivery_zone("korea japan") == "Korea / Japan"
    assert normalize_delivery_zone("amerique") is None
    assert normalize_delivery_zone("asie est") is None
    assert normalize_delivery_zone("unknown-zone") is None


def test_get_zone_manager_email_uses_canonical_zone_mapping():
    from app.services.routing import get_zone_manager_email

    assert get_zone_manager_email("Europe") == (
        "franck.lagadec@avocarbon.com",
        "Europe",
    )
    assert get_zone_manager_email("Africa") == (
        "franck.lagadec@avocarbon.com",
        "Africa",
    )
    assert get_zone_manager_email("India") == (
        "eipe.thomas@avocarbon.com",
        "India",
    )
    assert get_zone_manager_email("North America") == (
        "dean.hayward@avocarbon.com",
        "North America",
    )
    assert get_zone_manager_email("South America") == (
        "dean.hayward@avocarbon.com",
        "South America",
    )
    assert get_zone_manager_email("China / South Pacific") == (
        "tao.ren@avocarbon.com",
        "China / South Pacific",
    )
    assert get_zone_manager_email("Korea/Japan") == (
        "tao.ren@avocarbon.com",
        "Korea / Japan",
    )
    assert get_zone_manager_email("antarctica") == (None, None)
