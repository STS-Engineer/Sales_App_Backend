import httpx
import pytest

from app.utils import currency as currency_utils


class _FakeResponse:
    def __init__(self, payload=None, *, raise_error=None):
        self._payload = payload or {}
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, response=None, seen_urls=None, **kwargs):
        self._response = response
        self._seen_urls = seen_urls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        if self._seen_urls is not None:
            self._seen_urls.append(url)
        return self._response


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_short_circuits_for_eur(monkeypatch):
    class _ExplodingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("AsyncClient should not be called for EUR")

    monkeypatch.setattr(currency_utils.httpx, "AsyncClient", _ExplodingAsyncClient)

    assert await currency_utils.get_eur_exchange_rate("eur") == 1.0


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_fetches_live_rate(monkeypatch):
    seen_urls = []
    response = _FakeResponse({"rates": {"EUR": 0.92}})

    def _client_factory(*args, **kwargs):
        return _FakeAsyncClient(
            *args,
            response=response,
            seen_urls=seen_urls,
            **kwargs,
        )

    monkeypatch.setattr(currency_utils.httpx, "AsyncClient", _client_factory)

    rate = await currency_utils.get_eur_exchange_rate("usd")

    assert rate == 0.92
    assert seen_urls == [
        "https://api.frankfurter.app/latest?from=USD&to=EUR"
    ]


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_returns_fallback_and_logs_warning(
    monkeypatch,
    caplog,
):
    response = _FakeResponse(
        raise_error=httpx.HTTPStatusError(
            "boom",
            request=httpx.Request(
                "GET",
                "https://api.frankfurter.app/latest?from=ZZZ&to=EUR",
            ),
            response=httpx.Response(404),
        )
    )

    def _client_factory(*args, **kwargs):
        return _FakeAsyncClient(*args, response=response, **kwargs)

    monkeypatch.setattr(currency_utils.httpx, "AsyncClient", _client_factory)

    with caplog.at_level("WARNING"):
        rate = await currency_utils.get_eur_exchange_rate("zzz")

    assert rate == 1.0
    assert "Failed to fetch EUR exchange rate for ZZZ" in caplog.text


@pytest.mark.asyncio
async def test_get_eur_exchange_rate_returns_fallback_for_malformed_payload(
    monkeypatch,
    caplog,
):
    response = _FakeResponse({"rates": {}})

    def _client_factory(*args, **kwargs):
        return _FakeAsyncClient(*args, response=response, **kwargs)

    monkeypatch.setattr(currency_utils.httpx, "AsyncClient", _client_factory)

    with caplog.at_level("WARNING"):
        rate = await currency_utils.get_eur_exchange_rate("gbp")

    assert rate == 1.0
    assert "Failed to fetch EUR exchange rate for GBP" in caplog.text
