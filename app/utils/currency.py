import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_eur_exchange_rate(currency_code: str, db3: AsyncSession) -> float:
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

    try:
        result = await db3.execute(
            text(
                """
                SELECT rate
                FROM public.ecb_exchange_rates
                WHERE quote_currency = :currency
                ORDER BY ref_date DESC
                LIMIT 1
                """
            ),
            {"currency": normalized_code},
        )
        rate = result.scalar_one_or_none()
        if rate is None:
            logger.warning(
                "FX DB lookup returned no rate for %s; falling back to 1.0",
                normalized_code,
            )
            return 1.0

        rate_value = float(rate)
        if rate_value == 0.0:
            logger.warning(
                "FX DB lookup returned a zero rate for %s; falling back to 1.0",
                normalized_code,
            )
            return 1.0

        return 1.0 / rate_value
    except Exception as exc:
        logger.warning(
            "FX DB lookup failed for %s; falling back to 1.0 (%s)",
            normalized_code,
            str(exc),
        )
        return 1.0
