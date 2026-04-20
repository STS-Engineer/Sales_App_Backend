import logging

import httpx

logger = logging.getLogger(__name__)

FX_TIMEOUT_SECONDS = 5


async def get_eur_exchange_rate(currency_code: str) -> float:
    normalized_code = str(currency_code or "").strip().upper()
    if not normalized_code or normalized_code == "EUR":
        return 1.0

    url = f"https://api.frankfurter.app/latest?from={normalized_code}&to=EUR"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(FX_TIMEOUT_SECONDS)
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        data = response.json()
        rate = data["rates"]["EUR"]
        return float(rate)
    except Exception as exc:
        logger.warning(
            "Failed to fetch EUR exchange rate for %s; falling back to 1.0 (%s)",
            normalized_code,
            exc,
        )
        return 1.0
