import datetime
import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings
from app.models.rfq import Rfq

logger = logging.getLogger(__name__)

assembly_engine: AsyncEngine | None = None
if settings.async_db_url2 is not None:
    assembly_engine = create_async_engine(
        settings.async_db_url2,
        echo=True,
        future=True,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_timeout=30,
    )

async def sync_rfq_to_assembly(rfq: Rfq) -> bool:
    if assembly_engine is None:
        return False

    timestamp = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "rfq_id": rfq.rfq_id,
        "rfq_data": json.dumps(rfq.rfq_data or {}),
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    try:
        async with assembly_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO public.rfq (rfq_id, rfq_data, created_at, updated_at)
                    VALUES (:rfq_id, CAST(:rfq_data AS jsonb), :created_at, :updated_at)
                    ON CONFLICT (rfq_id) DO UPDATE
                    SET rfq_data = EXCLUDED.rfq_data,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                payload,
            )
        return True
    except Exception:
        logger.exception(
            "Assembly RFQ mirror failed for rfq_id=%s",
            rfq.rfq_id,
        )
        return False
