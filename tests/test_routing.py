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
        N2_ZONE_EMAIL,
        N2_AMERICAS_EMAIL,
        N1_VP_EMAIL,
        N0_CEO_EMAIL,
    )

    assert N2_ZONE_EMAIL == "franck.lagadec@avocarbon.com"
    assert N2_AMERICAS_EMAIL == "dean.hayward@avocarbon.com"
    assert N1_VP_EMAIL == "eric.suszylo@avocarbon.com"
    assert N0_CEO_EMAIL == "olivier.spicker@avocarbon.com"
