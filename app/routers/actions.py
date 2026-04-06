import os
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.contact import Contact
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User, UserRole
from app.services.audit import log_action

router = APIRouter(prefix="/api/actions", tags=["actions"])


class CheckGroupRequest(BaseModel):
    customer_name: str


class CheckContactRequest(BaseModel):
    email: str


class TriggerWorkflowRequest(BaseModel):
    rfq_id: str


async def _get_rfq_or_404(db: AsyncSession, rfq_id: str) -> Rfq:
    result = await db.execute(select(Rfq).where(Rfq.rfq_id == rfq_id))
    rfq = result.scalar_one_or_none()
    if not rfq:
        raise HTTPException(status_code=404, detail=f"RFQ '{rfq_id}' not found.")
    return rfq


@router.post("/check-group", operation_id="checkGroupeExistence")
async def check_group(
    body: CheckGroupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Contact).where(Contact.contact_name.ilike(f"%{body.customer_name}%"))
    )
    contacts = result.scalars().all()
    return {
        "exists": len(contacts) > 0,
        "matches": [
            {
                "id": contact.contact_id,
                "name": contact.contact_name,
                "email": contact.contact_email,
                "function": contact.contact_function,
            }
            for contact in contacts
        ],
    }


@router.post("/upload-file", operation_id="uploadRfqFiles")
async def upload_file(
    rfq_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    upload_dir = os.path.join("uploads", rfq_id)
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, os.path.basename(file.filename or "attachment"))
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"rfq_id": rfq_id, "filename": file.filename, "path": file_path}


@router.post("/check-contact", operation_id="checkContactExistence")
async def check_contact(
    body: CheckContactRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Contact).where(Contact.contact_email == body.email))
    contact = result.scalar_one_or_none()
    if not contact:
        return {"exists": False, "contact": None}
    return {
        "exists": True,
        "contact": {
            "id": contact.contact_id,
            "email": contact.contact_email,
            "name": contact.contact_name,
            "first_name": contact.contact_first_name,
            "function": contact.contact_function,
            "phone": contact.contact_phone,
        },
    }


@router.post("/trigger-workflow", operation_id="triggerValidationWorkflow")
async def trigger_workflow(
    body: TriggerWorkflowRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, body.rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to trigger this workflow.")

    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQ must be in RFQ/NEW_RFQ to trigger validation. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    rfq_data = dict(rfq.rfq_data or {})
    acronym = (rfq_data.get("product_line_acronym") or "").strip()
    zone_manager_email = (
        rfq_data.get("zone_manager_email") or rfq_data.get("validator_email") or ""
    ).strip()
    revision = str(rfq_data.get("revision_level") or "00").strip() or "00"

    if not acronym:
        raise HTTPException(status_code=400, detail="Missing product_line_acronym.")
    if not zone_manager_email:
        raise HTTPException(status_code=400, detail="Missing zone_manager_email.")

    count_query = await db.execute(
        select(func.count())
        .select_from(Rfq)
        .where(Rfq.product_line_acronym == acronym, Rfq.zone_manager_email.is_not(None))
    )
    current_count = count_query.scalar_one() or 0
    sequence = current_count + 1
    year = datetime.now().strftime("%y")
    systematic_rfq_id = f"{year}{sequence:03d}-{acronym}-{revision}"

    rfq.product_line_acronym = acronym
    rfq.zone_manager_email = zone_manager_email
    rfq.phase = RfqPhase.RFQ
    rfq.sub_status = RfqSubStatus.PENDING_FOR_VALIDATION
    rfq_data["zone_manager_email"] = zone_manager_email
    rfq_data.pop("validator_email", None)
    rfq_data["systematic_rfq_id"] = systematic_rfq_id
    rfq.rfq_data = rfq_data

    await log_action(
        db,
        rfq.rfq_id,
        f"Validation workflow triggered -> {rfq.phase.value}/{rfq.sub_status.value}",
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)

    return {
        "temp_rfq_id": body.rfq_id,
        "final_rfq_id": rfq.rfq_id,
        "systematic_rfq_id": systematic_rfq_id,
        "phase": rfq.phase.value,
        "sub_status": rfq.sub_status.value,
    }


@router.post("/upload-costing", operation_id="uploadCostingFiles")
async def upload_costing_file(
    rfq_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    costing_states = {
        (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY),
        (RfqPhase.COSTING, RfqSubStatus.PRICING),
    }
    if (rfq.phase, rfq.sub_status) not in costing_states:
        raise HTTPException(
            status_code=400,
            detail=(
                "File uploads are only allowed during the costing phase. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    upload_dir = os.path.join("uploads", rfq_id, "costing")
    os.makedirs(upload_dir, exist_ok=True)
    filename = os.path.basename(file.filename or "attachment")
    file_path = os.path.join(upload_dir, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_meta = {
        "filename": filename,
        "path": file_path,
        "uploaded_by": current_user.email,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }

    rfq.costing_files = (rfq.costing_files or []) + [file_meta]
    await log_action(db, rfq_id, f"Costing file uploaded: {filename}", current_user.email)
    await db.commit()
    await db.refresh(rfq)

    return {"rfq_id": rfq_id, "file": file_meta, "total_files": len(rfq.costing_files)}
