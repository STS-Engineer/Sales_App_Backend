"""
Test module for the routing (escalation) service.

These are pure unit tests — no DB required.
"""
import pytest


def test_calculate_pte():
    from app.services.routing import calculate_pte

    # 10 EUR * 50_000 = 500_000 → 500 KEUR
    assert calculate_pte(10.0, 50_000) == 500.0

    # 0.5 EUR * 100 = 50 → 0.05 KEUR
    assert abs(calculate_pte(0.5, 100) - 0.05) < 1e-9

    # 200 EUR * 1000 = 200_000 → 200 KEUR
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
    assert N0_CEO_EMAIL == "olivier.spicker@avocarbon.com"


def test_normalize_delivery_zone_handles_legacy_aliases():
    from app.services.routing import normalize_delivery_zone

    assert normalize_delivery_zone("asie est") == "asie est"
    assert normalize_delivery_zone("east asia") == "asie est"
    assert normalize_delivery_zone("south asia") == "asie sud"
    assert normalize_delivery_zone("Europe") == "europe"
    assert normalize_delivery_zone("amérique") == "amerique"
    assert normalize_delivery_zone("unknown-zone") is None


def test_get_zone_manager_email_uses_canonical_zone_mapping():
    from app.services.routing import get_zone_manager_email

    assert get_zone_manager_email("europe") == (
        "franck.lagadec@avocarbon.com",
        "europe",
    )
    assert get_zone_manager_email("america") == (
        "dean.hayward@avocarbon.com",
        "amerique",
    )
    assert get_zone_manager_email("east asia") == (
        "tao.ren@avocarbon.com",
        "asie est",
    )
    assert get_zone_manager_email("south asia") == (
        "eipe.thomas@avocarbon.com",
        "asie sud",
    )
    assert get_zone_manager_email("antarctica") == (None, None)
