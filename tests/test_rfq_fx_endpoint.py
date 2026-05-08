import pytest

from app.routers import rfq as rfq_router


@pytest.mark.asyncio
async def test_get_rfq_eur_fx_rate_short_circuits_for_eur(monkeypatch):
    fx_calls: list[str] = []

    async def _eur_get_rate(currency_code, *, db3):
        assert db3 is not None
        fx_calls.append(currency_code)
        return 1.0

    monkeypatch.setattr(rfq_router, "get_eur_exchange_rate", _eur_get_rate)

    payload = await rfq_router.get_rfq_eur_fx_rate(
        currency_code="eur",
        db3=object(),
        current_user=object(),
    )

    assert payload.model_dump() == {
        "currency_code": "EUR",
        "eur_rate": 1.0,
        "fallback_used": False,
    }
    assert fx_calls == ["EUR"]


@pytest.mark.asyncio
async def test_get_rfq_eur_fx_rate_uses_existing_utility_for_non_eur(monkeypatch):
    fx_calls: list[str] = []

    async def _fake_get_rate(currency_code, db3):
        assert db3 is not None
        fx_calls.append(currency_code)
        return 0.91

    monkeypatch.setattr(rfq_router, "get_eur_exchange_rate", _fake_get_rate)

    payload = await rfq_router.get_rfq_eur_fx_rate(
        currency_code="u$s$d",
        db3=object(),
        current_user=object(),
    )

    assert payload.model_dump() == {
        "currency_code": "USD",
        "eur_rate": 0.91,
        "fallback_used": False,
    }
    assert fx_calls == ["USD"]


@pytest.mark.asyncio
async def test_get_rfq_eur_fx_rate_flags_non_eur_fallback(monkeypatch):
    async def _fallback_get_rate(currency_code, db3):
        assert currency_code == "MXN"
        assert db3 is not None
        return 1.0

    monkeypatch.setattr(rfq_router, "get_eur_exchange_rate", _fallback_get_rate)

    payload = await rfq_router.get_rfq_eur_fx_rate(
        currency_code="mxn",
        db3=object(),
        current_user=object(),
    )

    assert payload.model_dump() == {
        "currency_code": "MXN",
        "eur_rate": 1.0,
        "fallback_used": True,
    }
