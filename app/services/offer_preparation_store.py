from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.offer_preparation import OfferPreparation
from app.models.rfq import Rfq
from app.services.offer_template import OFFER_PREPARATION_DATA_KEY

LEGACY_OFFER_CHAT_HISTORY_KEY = "offer_chat_history"
LEGACY_OFFER_KEYS = (
    OFFER_PREPARATION_DATA_KEY,
    LEGACY_OFFER_CHAT_HISTORY_KEY,
)


def _build_legacy_offer_data_snapshot(rfq: Rfq) -> dict[str, Any]:
    legacy_value = (rfq.rfq_data or {}).get(OFFER_PREPARATION_DATA_KEY)
    return dict(legacy_value) if isinstance(legacy_value, dict) else {}


def _build_legacy_offer_chat_history_snapshot(rfq: Rfq) -> list[dict[str, Any]]:
    legacy_value = (rfq.rfq_data or {}).get(LEGACY_OFFER_CHAT_HISTORY_KEY)
    if not isinstance(legacy_value, list):
        return []
    return [dict(entry) for entry in legacy_value if isinstance(entry, dict)]


def clear_legacy_offer_state_from_rfq(rfq: Rfq) -> bool:
    if not isinstance(rfq.rfq_data, dict):
        return False

    next_rfq_data = dict(rfq.rfq_data)
    changed = False
    for key in LEGACY_OFFER_KEYS:
        if key in next_rfq_data:
            next_rfq_data.pop(key, None)
            changed = True

    if changed:
        rfq.rfq_data = next_rfq_data
    return changed


def get_offer_preparation_data_snapshot(
    rfq: Rfq,
    offer_preparation: OfferPreparation | None = None,
) -> dict[str, Any]:
    offer_record = offer_preparation or rfq.offer_preparation
    if offer_record is not None and isinstance(offer_record.offer_data, dict):
        persisted_offer_data = dict(offer_record.offer_data)
        if persisted_offer_data:
            return persisted_offer_data

    return _build_legacy_offer_data_snapshot(rfq)


def get_offer_chat_history_snapshot(
    rfq: Rfq,
    offer_preparation: OfferPreparation | None = None,
) -> list[dict[str, Any]]:
    offer_record = offer_preparation or rfq.offer_preparation
    if offer_record is not None and isinstance(offer_record.chat_history, list):
        return [dict(entry) for entry in offer_record.chat_history if isinstance(entry, dict)]
    return []


async def get_or_create_offer_preparation(
    db: AsyncSession,
    rfq: Rfq,
) -> OfferPreparation:
    legacy_offer_data = _build_legacy_offer_data_snapshot(rfq)
    legacy_chat_history = _build_legacy_offer_chat_history_snapshot(rfq)

    if rfq.offer_preparation is not None:
        offer_preparation = rfq.offer_preparation
        if (offer_preparation.offer_data is None or not offer_preparation.offer_data) and legacy_offer_data:
            offer_preparation.offer_data = legacy_offer_data
        if (offer_preparation.chat_history is None or not offer_preparation.chat_history) and legacy_chat_history:
            offer_preparation.chat_history = legacy_chat_history
        clear_legacy_offer_state_from_rfq(rfq)
        return offer_preparation

    offer_preparation = OfferPreparation(
        rfq_id=rfq.rfq_id,
        offer_data=legacy_offer_data or None,
        chat_history=legacy_chat_history or None,
    )
    db.add(offer_preparation)
    rfq.offer_preparation = offer_preparation
    clear_legacy_offer_state_from_rfq(rfq)
    await db.flush()
    return offer_preparation
