import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rfq import Rfq


async def generate_rfq_id(acronym: str, db: AsyncSession) -> str:
    """
    Generates the next sequential RFQ ID for a given product line acronym.

    Format: YY<seq:03d>-<ACRONYM>-00
    Example: 26001-ASS-00, 26002-ASS-00, 26001-BRU-00

    Sequence resets per acronym (not globally), starting from 1.
    """
    year = str(datetime.now().year)[2:]  # e.g. "26"
    pattern = f"{year}%-{acronym}-__"

    result = await db.execute(
        select(Rfq.rfq_id).where(Rfq.rfq_id.like(pattern))
    )
    existing_ids = result.scalars().all()

    max_seq = 0
    regex = re.compile(rf"^{year}(\d+)-{re.escape(acronym)}-\d{{2}}$")
    for rfq_id in existing_ids:
        match = regex.match(rfq_id)
        if match:
            seq = int(match.group(1))
            if seq > max_seq:
                max_seq = seq

    next_seq = max_seq + 1
    return f"{year}{next_seq:03d}-{acronym}-00"
