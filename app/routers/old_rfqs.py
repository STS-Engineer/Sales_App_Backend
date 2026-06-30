import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.old_rfq_raw import OldRfqRaw
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/old-rfqs", tags=["old-rfqs"])


def _serialize_row(row: OldRfqRaw) -> dict:
    return {
        column.name: getattr(row, column.name)
        for column in OldRfqRaw.__table__.columns
    }


@router.get("")
async def get_old_rfqs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OldRfqRaw).order_by(
            OldRfqRaw.excel_row_number.asc(),
            OldRfqRaw.import_id.asc(),
        )
    )
    rows = result.scalars().all()

    projects = []
    current_project: dict | None = None

    for row in rows:
        if row.row_type == "project":
            project_data = _serialize_row(row)
            project_data["subitems"] = []
            current_project = project_data
            projects.append(project_data)
        elif row.row_type == "subitem":
            if current_project is None:
                continue
            current_project["subitems"].append(_serialize_row(row))
        # empty rows are ignored

    for project in projects:
        project["subitems_count"] = len(project["subitems"])

    return {"items": projects, "total": len(projects)}
