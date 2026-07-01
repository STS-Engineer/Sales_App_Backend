import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.old_rfqs import OldRfqMonday, OldRfqSubitem
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/old-rfqs", tags=["old-rfqs"])


def _model_columns(model) -> list:
    return [column.name for column in model.__table__.columns]


def _serialize_row(row) -> dict:
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
    }


@router.get("")
async def get_old_rfqs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OldRfqMonday)
        .options(selectinload(OldRfqMonday.subitems))
        .order_by(
            OldRfqMonday.excel_row_number.asc(),
            OldRfqMonday.old_rfq_id.asc(),
        )
    )
    rfqs = result.scalars().unique().all()

    items = []
    for rfq in rfqs:
        rfq_data = _serialize_row(rfq)
        ordered_subitems = sorted(
            list(rfq.subitems or []),
            key=lambda s: (
                s.subitem_order if s.subitem_order is not None else 999999,
                s.excel_row_number if s.excel_row_number is not None else 999999,
                s.old_rfq_subitem_id,
            ),
        )
        rfq_data["subitems"] = [_serialize_row(sub) for sub in ordered_subitems]
        rfq_data["subitems_count"] = len(rfq_data["subitems"])
        items.append(rfq_data)

    return {
        "items": items,
        "total": len(items),
        "project_columns": _model_columns(OldRfqMonday) + ["subitems_count"],
        "subitem_columns": _model_columns(OldRfqSubitem),
    }
