from datetime import datetime
from typing import Any

from pydantic import BaseModel


class OfferPreparationOut(BaseModel):
    rfq_id: str
    offer_data: dict[str, Any] | None
    chat_history: list[dict[str, Any]] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
