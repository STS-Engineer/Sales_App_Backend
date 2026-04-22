import pytest

from app.utils import currency as currency_utils


class _FakeResult:
    def __init__(self, rate):
        self._rate = rate

    def scalar_one_or_none(self):
        return self._rate


class _FakeDb:
    def __init__(self, rate=None, *, error=None):
        self._rate = rate
        self._error = error
        self.executed = False
        self.statement = None
        self.params = None

    async def execute(self, statement, params):
        self.executed = True
        self.statement = statement
        self.params = params
        if self._error is not None:
            raise self._error
        return _FakeResult(self._rate)


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_short_circuits_for_eur():
    db3 = _FakeDb(error=AssertionError("db3 should not be queried for EUR"))

    assert await currency_utils.get_eur_exchange_rate("eur", db3=db3) == 1.0
    assert db3.executed is False


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_reads_latest_rate_from_db():
    db3 = _FakeDb(rate=1.25)

    rate = await currency_utils.get_eur_exchange_rate("usd", db3=db3)

    assert rate == pytest.approx(0.8)
    assert db3.executed is True
    assert "SELECT rate" in str(db3.statement)
    assert "FROM public.ecb_exchange_rates" in str(db3.statement)
    assert db3.params == {"currency": "USD"}


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_returns_fallback_and_logs_warning_when_missing(
    caplog,
):
    db3 = _FakeDb(rate=None)

    with caplog.at_level("WARNING"):
        rate = await currency_utils.get_eur_exchange_rate("zzz", db3=db3)

    assert rate == 1.0
    assert "FX DB lookup returned no rate for ZZZ" in caplog.text


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_returns_fallback_and_logs_warning_on_db_error(
    caplog,
):
    db3 = _FakeDb(error=RuntimeError("secondary db unavailable"))

    with caplog.at_level("WARNING"):
        rate = await currency_utils.get_eur_exchange_rate("gbp", db3=db3)

    assert rate == 1.0
    assert "FX DB lookup failed for GBP" in caplog.text
