import logging
import re

import httpx

logger = logging.getLogger(__name__)

FX_TIMEOUT_SECONDS = 5


async def get_eur_exchange_rate(currency_code: str) -> float:
    normalized_code = re.sub(r"[^A-Za-z]", "", str(currency_code or "")).upper()
    if len(normalized_code) != 3:
        logger.warning(
            "FX lookup skipped because the sanitized currency code is invalid: %r -> %r",
            currency_code,
            normalized_code,
        )
        return 1.0

    if not normalized_code or normalized_code == "EUR":
        return 1.0

    url = f"https://api.frankfurter.dev/v1/latest?from={normalized_code}&to=EUR"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(FX_TIMEOUT_SECONDS),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        data = response.json()
        rate = data["rates"]["EUR"]
        return float(rate)
    except Exception as exc:
        logger.warning(
            "FX API failed for %s; falling back to 1.0 (%s)",
            normalized_code,
            str(exc),
        )
        return 1.0
