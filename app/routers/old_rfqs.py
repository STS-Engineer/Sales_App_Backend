import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.old_rfqs import OldRfqMonday, OldRfqSubitem
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/old-rfqs", tags=["old-rfqs"])
subitem_router = APIRouter(prefix="/api/old-rfq-subitems", tags=["old-rfqs"])


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


# Columns that must never be overwritten by the PUT endpoint.
_PROTECTED_COLUMNS = {"old_rfq_id", "excel_row_number"}

_EDITABLE_COLUMNS = {
    col.name
    for col in OldRfqMonday.__table__.columns
    if col.name not in _PROTECTED_COLUMNS
}


@router.put("/{old_rfq_id}")
async def update_old_rfq(
    old_rfq_id: int,
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OldRfqMonday).where(OldRfqMonday.old_rfq_id == old_rfq_id)
    )
    old_rfq = result.scalar_one_or_none()

    if old_rfq is None:
        raise HTTPException(status_code=404, detail="Old RFQ not found.")

    for key, value in payload.items():
        if key in _EDITABLE_COLUMNS:
            setattr(old_rfq, key, value)

    await db.commit()
    await db.refresh(old_rfq)

    return {
        "item": _serialize_row(old_rfq),
    }


# Subitem protected columns — these are never overwritten.
_PROTECTED_SUBITEM_COLUMNS = {
    "old_rfq_subitem_id",
    "old_rfq_id",
    "excel_row_number",
    "subitem_order",
    "parent_id",
}

_EDITABLE_SUBITEM_COLUMNS = {
    col.name
    for col in OldRfqSubitem.__table__.columns
    if col.name not in _PROTECTED_SUBITEM_COLUMNS
}


@subitem_router.put("/{old_rfq_subitem_id}")
async def update_old_rfq_subitem(
    old_rfq_subitem_id: int,
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OldRfqSubitem).where(OldRfqSubitem.old_rfq_subitem_id == old_rfq_subitem_id)
    )
    subitem = result.scalar_one_or_none()

    if subitem is None:
        raise HTTPException(status_code=404, detail="Old RFQ subitem not found.")

    for key, value in payload.items():
        if key in _EDITABLE_SUBITEM_COLUMNS:
            setattr(subitem, key, value)

    await db.commit()
    await db.refresh(subitem)

    return {
        "item": _serialize_row(subitem),
    }
