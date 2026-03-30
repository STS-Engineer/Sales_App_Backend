import datetime
import os
import shutil
import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.audit_log import AuditLog
from app.models.rfq import ALLOWED_TRANSITIONS, Rfq, RfqPhase, RfqSubStatus, VALID_PHASE_SUBSTATUS
from app.models.user import User, UserRole
from app.schemas.rfq import (
    AdvanceStatusRequest,
    AuditLogOut,
    AutopsyRequest,
    CostingReviewRequest,
    PhaseStatusUpdateRequest,
    RfqCreateRequest,
    RfqDataUpdateRequest,
    RfqOut,
    ValidateRfqRequest,
)
from app.services.audit import log_action

router = APIRouter(prefix="/api/rfq", tags=["rfq"])

SMTP_SERVER = "avocarbon-com.mail.protection.outlook.com"
SMTP_PORT = 25
SMTP_FROM = "administration.STS@avocarbon.com"
TERMINAL_SUBSTATUSES = {RfqSubStatus.LOST, RfqSubStatus.CANCELED}


def _ensure_valid_phase_sub_status(phase: RfqPhase, sub_status: RfqSubStatus) -> None:
    valid_sub_statuses = VALID_PHASE_SUBSTATUS.get(phase, set())
    if sub_status not in valid_sub_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid phase/sub-status pair: {phase.value}/{sub_status.value}.",
        )


def _set_phase_sub_status(rfq: Rfq, phase: RfqPhase, sub_status: RfqSubStatus) -> None:
    _ensure_valid_phase_sub_status(phase, sub_status)
    rfq.phase = phase
    rfq.sub_status = sub_status


def _can_view_rfq(current_user: User, rfq: Rfq) -> bool:
    return (
        current_user.role == UserRole.OWNER
        or rfq.created_by_email == current_user.email
        or rfq.zone_manager_email == current_user.email
    )


def _assert_can_view_rfq(current_user: User, rfq: Rfq) -> None:
    if not _can_view_rfq(current_user, rfq):
        raise HTTPException(status_code=403, detail="Not authorized to access this RFQ.")


async def _get_rfq_or_404(db: AsyncSession, rfq_id: str) -> Rfq:
    result = await db.execute(select(Rfq).where(Rfq.rfq_id == rfq_id))
    rfq = result.scalar_one_or_none()
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found.")
    return rfq


def _allowed_transitions_for(rfq: Rfq) -> set[tuple[RfqPhase, RfqSubStatus]]:
    allowed = set(ALLOWED_TRANSITIONS.get((rfq.phase, rfq.sub_status), set()))

    # Business clarification: a "mission not accepted" outcome must close the RFQ
    # immediately with LOST or CANCELED plus autopsy notes.
    if (rfq.phase, rfq.sub_status) == (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED):
        allowed.update(
            {
                (RfqPhase.CLOSED, RfqSubStatus.LOST),
                (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
            }
        )

    return allowed


async def _generate_systematic_rfq_id(
    db: AsyncSession,
    acronym: str,
    revision: str,
) -> str:
    count_query = await db.execute(
        select(func.count())
        .select_from(Rfq)
        .where(Rfq.product_line_acronym == acronym, Rfq.zone_manager_email.is_not(None))
    )
    current_count = count_query.scalar_one() or 0
    yy = datetime.datetime.now().strftime("%y")
    return f"{yy}{current_count + 1:03d}-{acronym}-{revision}"


async def _maybe_assign_systematic_rfq_id(
    db: AsyncSession,
    rfq: Rfq,
    rfq_data: dict,
) -> dict:
    next_data = dict(rfq_data)
    if next_data.get("systematic_rfq_id"):
        return next_data

    acronym = (next_data.get("product_line_acronym") or rfq.product_line_acronym or "").strip()
    zone_manager_email = (
        next_data.get("zone_manager_email")
        or next_data.get("validator_email")
        or rfq.zone_manager_email
        or ""
    ).strip()
    revision = str(next_data.get("revision_level") or "00").strip() or "00"

    if not acronym or not zone_manager_email:
        return next_data

    next_data["product_line_acronym"] = acronym
    next_data["zone_manager_email"] = zone_manager_email
    next_data.pop("validator_email", None)
    next_data["systematic_rfq_id"] = await _generate_systematic_rfq_id(db, acronym, revision)
    rfq.product_line_acronym = acronym
    rfq.zone_manager_email = zone_manager_email
    return next_data


def _send_validation_email(
    zone_manager_email: str,
    systematic_rfq_id: str,
    acronym: str,
    rfq_id: str,
) -> None:
    frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
    rfq_link = f"{frontend_url}/rfq/{rfq_id}"

    msg = EmailMessage()
    msg["Subject"] = f"Action Required: Zone Manager Review for RFQ {systematic_rfq_id}"
    msg["From"] = SMTP_FROM
    msg["To"] = zone_manager_email

    text_body = f"""Hello,

A new RFQ ({systematic_rfq_id}) for the {acronym} product line has been submitted.

You have been assigned as the Zone Manager for this RFQ.
It requires your validation in order to proceed to the Costing phase.
Please log into the AVO Carbon RFQ Portal to review the details:
{rfq_link}

Best regards,
RFQ Automated System
"""
    msg.set_content(text_body)

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333333; line-height: 1.6; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; border: 1px solid #e0e0e0;">
          <h2 style="color: #1a365d; margin-top: 0;">Zone Manager Review Required</h2>
          <p>Hello,</p>
          <p>A new RFQ <strong>({systematic_rfq_id})</strong> for the <strong>{acronym}</strong> product line has been submitted and requires your approval as the <strong>Zone Manager</strong> to proceed to the Costing phase.</p>

          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Review RFQ
            </a>
          </div>

          <p style="font-size: 14px; color: #666666;">
            If the button above does not work, copy and paste this link into your browser:<br>
            <a href="{rfq_link}" style="color: #2563eb; word-break: break-all;">{rfq_link}</a>
          </p>

          <hr style="border: none; border-top: 1px solid #eeeeee; margin: 30px 0;">
          <p style="font-size: 12px; color: #999999; margin-bottom: 0;">
            Best regards,<br>
            <strong>AVO Carbon RFQ System</strong>
          </p>
        </div>
      </body>
    </html>
    """
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.send_message(msg)
    except Exception as exc:
        print(f"SMTP Error: {exc}")


@router.post("", response_model=RfqOut, status_code=201)
async def create_rfq(
    body: RfqCreateRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_role(UserRole.COMMERCIAL, UserRole.OWNER, UserRole.ZONE_MANAGER)
    ),
):
    request_body = body or RfqCreateRequest()
    chat_mode = request_body.chat_mode.lower().strip()
    initial_sub_status = (
        RfqSubStatus.POTENTIAL if chat_mode == "potential" else RfqSubStatus.NEW_RFQ
    )
    rfq_data = dict(request_body.rfq_data or {})
    zone_manager_email = (
        rfq_data.get("zone_manager_email") or rfq_data.get("validator_email") or None
    )

    rfq = Rfq(
        phase=RfqPhase.RFQ,
        sub_status=initial_sub_status,
        product_line_acronym=rfq_data.get("product_line_acronym"),
        zone_manager_email=zone_manager_email,
        created_by_email=current_user.email,
        rfq_data=rfq_data,
        chat_history=[],
    )
    rfq.rfq_data = await _maybe_assign_systematic_rfq_id(db, rfq, rfq_data)
    db.add(rfq)

    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.put("/{rfq_id}/data", response_model=RfqOut)
async def update_rfq_data(
    rfq_id: str,
    body: RfqDataUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to update this RFQ.")

    next_data = dict(rfq.rfq_data or {})
    next_data.update(body.rfq_data)
    rfq.rfq_data = next_data

    if "product_line_acronym" in body.rfq_data:
        rfq.product_line_acronym = body.rfq_data.get("product_line_acronym")
    if "zone_manager_email" in body.rfq_data or "validator_email" in body.rfq_data:
        rfq.zone_manager_email = (
            body.rfq_data.get("zone_manager_email") or body.rfq_data.get("validator_email")
        )

    rfq.rfq_data = await _maybe_assign_systematic_rfq_id(db, rfq, next_data)

    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.post("/{rfq_id}/upload")
async def upload_rfq_file(
    rfq_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to upload files to this RFQ.")

    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = os.path.basename(file.filename or "attachment")
    stored_name = f"{rfq_id}_{safe_name}"
    file_path = os.path.join(upload_dir, stored_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_meta = {
        "filename": safe_name,
        "path": f"/api/rfq/download/{stored_name}",
        "uploaded_by": current_user.email,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    extracted_data = dict(rfq.rfq_data or {})
    existing_files = list(extracted_data.get("rfq_files") or [])
    existing_files.append(file_meta)
    extracted_data["rfq_files"] = existing_files
    extracted_data["rfq_file_path"] = file_meta["path"]
    rfq.rfq_data = extracted_data

    await db.commit()
    await db.refresh(rfq)

    return {
        "message": "File uploaded successfully",
        "file_path": file_meta["path"],
        "file": file_meta,
    }


@router.get("/download/{filename}")
async def download_file(filename: str):
    file_path = os.path.join("uploads", filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path)


@router.get("", response_model=list[RfqOut])
async def list_rfqs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Rfq).order_by(Rfq.updated_at.desc(), Rfq.created_at.desc())

    if current_user.role != UserRole.OWNER:
        query = query.where(
            or_(
                Rfq.created_by_email == current_user.email,
                Rfq.zone_manager_email == current_user.email,
            )
        )

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{rfq_id}", response_model=RfqOut)
async def get_rfq(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_view_rfq(current_user, rfq)
    return rfq


@router.post("/{rfq_id}/submit")
async def submit_rfq_for_validation(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to submit this RFQ.")

    if rfq.phase != RfqPhase.RFQ or rfq.sub_status not in {
        RfqSubStatus.POTENTIAL,
        RfqSubStatus.NEW_RFQ,
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "Only RFQs in RFQ/POTENTIAL or RFQ/NEW_RFQ can be submitted for validation. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    extracted_data = dict(rfq.rfq_data or {})
    acronym = (extracted_data.get("product_line_acronym") or "").strip()
    revision = str(extracted_data.get("revision_level") or "00").strip() or "00"
    zone_manager_email = (
        extracted_data.get("zone_manager_email") or extracted_data.get("validator_email") or ""
    ).strip()

    if not acronym:
        raise HTTPException(
            status_code=400,
            detail="Product line acronym is missing. Cannot submit this RFQ.",
        )
    if not zone_manager_email:
        raise HTTPException(
            status_code=400,
            detail="Zone Manager email is missing. Cannot submit this RFQ.",
        )

    extracted_data["product_line_acronym"] = acronym
    extracted_data["zone_manager_email"] = zone_manager_email
    extracted_data.pop("validator_email", None)
    if not extracted_data.get("systematic_rfq_id"):
        extracted_data["systematic_rfq_id"] = await _generate_systematic_rfq_id(
            db, acronym, revision
        )
    systematic_rfq_id = extracted_data["systematic_rfq_id"]
    rfq.rfq_data = extracted_data
    rfq.zone_manager_email = zone_manager_email
    rfq.product_line_acronym = acronym
    _set_phase_sub_status(rfq, RfqPhase.RFQ, RfqSubStatus.IN_VALIDATION)

    await log_action(
        db,
        rfq_id,
        f"RFQ submitted for validation -> {RfqPhase.RFQ.value}/{RfqSubStatus.IN_VALIDATION.value}",
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)

    _send_validation_email(zone_manager_email, systematic_rfq_id, acronym, rfq_id)

    return {
        "message": "RFQ submitted for validation.",
        "systematic_rfq_id": systematic_rfq_id,
        "phase": rfq.phase.value,
        "sub_status": rfq.sub_status.value,
    }


@router.put("/{rfq_id}/status", response_model=RfqOut)
async def update_rfq_status(
    rfq_id: str,
    body: PhaseStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_role(UserRole.ZONE_MANAGER, UserRole.OWNER, UserRole.COSTING_TEAM)
    ),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _ensure_valid_phase_sub_status(body.phase, body.sub_status)
    _set_phase_sub_status(rfq, body.phase, body.sub_status)

    await log_action(
        db,
        rfq_id,
        f"Status updated to {body.phase.value}/{body.sub_status.value}",
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.post("/{rfq_id}/autopsy", response_model=RfqOut)
async def submit_autopsy(
    rfq_id: str,
    body: AutopsyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_view_rfq(current_user, rfq)

    if rfq.sub_status not in TERMINAL_SUBSTATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Autopsy can only be submitted for LOST or CANCELED RFQs. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    rfq.rejection_reason = body.rejection_reason
    rfq.autopsy_notes = body.autopsy_notes
    await log_action(db, rfq_id, "Autopsy submitted", current_user.email)
    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.get("/{rfq_id}/audit-logs", response_model=list[AuditLogOut])
async def get_audit_logs(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_view_rfq(current_user, rfq)

    query = select(AuditLog).where(AuditLog.rfq_id == rfq_id).order_by(AuditLog.timestamp.desc())
    logs = await db.execute(query)
    return logs.scalars().all()


@router.post("/{rfq_id}/validate", response_model=RfqOut)
async def validate_rfq(
    rfq_id: str,
    body: ValidateRfqRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.ZONE_MANAGER, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.zone_manager_email != current_user.email:
        raise HTTPException(
            status_code=403,
            detail="You are not assigned as the Zone Manager for this RFQ.",
        )

    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.IN_VALIDATION):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQ must be in RFQ/IN_VALIDATION before it can be validated. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    if body.approved:
        _set_phase_sub_status(rfq, RfqPhase.COSTING, RfqSubStatus.FEASIBILITY)
        await log_action(
            db,
            rfq_id,
            f"Zone Manager approved -> {RfqPhase.COSTING.value}/{RfqSubStatus.FEASIBILITY.value}",
            current_user.email,
        )
    else:
        _set_phase_sub_status(rfq, RfqPhase.CLOSED, RfqSubStatus.LOST)
        rfq.rejection_reason = body.rejection_reason
        await log_action(
            db,
            rfq_id,
            f"Zone Manager rejected -> {RfqPhase.CLOSED.value}/{RfqSubStatus.LOST.value}: {body.rejection_reason}",
            current_user.email,
        )

    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.post("/{rfq_id}/costing_review", response_model=RfqOut)
async def costing_review(
    rfq_id: str,
    body: CostingReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if (rfq.phase, rfq.sub_status) != (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQ must be in COSTING/FEASIBILITY before costing review. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    if body.is_feasible:
        _set_phase_sub_status(rfq, RfqPhase.COSTING, RfqSubStatus.PRICING)
        await log_action(
            db,
            rfq_id,
            f"Costing review approved -> {RfqPhase.COSTING.value}/{RfqSubStatus.PRICING.value}",
            current_user.email,
        )
    else:
        _set_phase_sub_status(rfq, RfqPhase.CLOSED, RfqSubStatus.LOST)
        rfq.rejection_reason = body.rejection_reason
        await log_action(
            db,
            rfq_id,
            f"Costing review rejected -> {RfqPhase.CLOSED.value}/{RfqSubStatus.LOST.value}: {body.rejection_reason}",
            current_user.email,
        )

    await db.commit()
    await db.refresh(rfq)
    return rfq


@router.post("/{rfq_id}/advance", response_model=RfqOut)
async def advance_status(
    rfq_id: str,
    body: AdvanceStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_role(
            UserRole.COMMERCIAL,
            UserRole.ZONE_MANAGER,
            UserRole.COSTING_TEAM,
            UserRole.PLANT_MANAGER,
            UserRole.PLM,
            UserRole.OWNER,
        )
    ),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if body.target_sub_status == RfqSubStatus.MISSION_NOT_ACCEPTED:
        raise HTTPException(
            status_code=400,
            detail=(
                "MISSION_NOT_ACCEPTED must close the RFQ immediately. "
                "Use CLOSED/LOST or CLOSED/CANCELED with autopsy_notes instead."
            ),
        )

    target_state = (body.target_phase, body.target_sub_status)
    allowed = _allowed_transitions_for(rfq)
    if target_state not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"Cannot advance from {rfq.phase.value}/{rfq.sub_status.value} "
                    f"to {body.target_phase.value}/{body.target_sub_status.value}."
                ),
                "allowed": [
                    {"phase": phase.value, "sub_status": sub_status.value}
                    for phase, sub_status in allowed
                ],
            },
        )

    _set_phase_sub_status(rfq, body.target_phase, body.target_sub_status)

    if body.target_sub_status in TERMINAL_SUBSTATUSES:
        rfq.autopsy_notes = body.autopsy_notes
        if body.notes and not rfq.rejection_reason:
            rfq.rejection_reason = body.notes

    note_suffix = f" | Notes: {body.notes}" if body.notes else ""
    await log_action(
        db,
        rfq_id,
        (
            f"Status advanced to {body.target_phase.value}/{body.target_sub_status.value}"
            f"{note_suffix}"
        ),
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)
    return rfq
