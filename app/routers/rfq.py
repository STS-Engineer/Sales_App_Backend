import datetime
import os
import smtplib
import uuid
from email.message import EmailMessage
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas
from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.audit_log import AuditLog
from app.models.discussion import DiscussionMessage
from app.models.potential import Potential
from app.models.rfq import ALLOWED_TRANSITIONS, Rfq, RfqPhase, RfqSubStatus, VALID_PHASE_SUBSTATUS
from app.models.user import User, UserRole
from app.schemas.discussion import DiscussionMessageCreateRequest, DiscussionMessageOut
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
    rfq_data_payload_to_dict,
)
from app.services.audit import log_action
from app.services.costing_template import (
    build_costing_template_filename,
    render_costing_template_pdf,
)
from app.services.potential import (
    get_missing_potential_shared_fields,
    sync_potential_to_rfq_data,
)

router = APIRouter(prefix="/api/rfq", tags=["rfq"])

SMTP_SERVER = "avocarbon-com.mail.protection.outlook.com"
SMTP_PORT = 25
SMTP_FROM = "administration.STS@avocarbon.com"
TERMINAL_SUBSTATUSES = {RfqSubStatus.LOST, RfqSubStatus.CANCELED}
RFQ_FILES_CONTAINER = "rfq-files"


@lru_cache(maxsize=1)
def _get_blob_service_client() -> BlobServiceClient:
    if not settings.azure_connection_string:
        raise RuntimeError("AZURE_CONNECTION_STRING is not configured.")
    return BlobServiceClient.from_connection_string(settings.azure_connection_string)


def _get_rfq_files_container_client():
    container_client = _get_blob_service_client().get_container_client(RFQ_FILES_CONTAINER)
    try:
        if not container_client.exists():
            container_client.create_container()
    except ResourceExistsError:
        pass
    return container_client


@lru_cache(maxsize=1)
def _get_azure_connection_parts() -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in settings.azure_connection_string.split(";"):
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        parts[key.strip()] = value
    return parts


def _build_blob_access_url(blob_name: str) -> str:
    blob_client = _get_rfq_files_container_client().get_blob_client(blob_name)
    connection_parts = _get_azure_connection_parts()
    account_name = connection_parts.get("AccountName", "")
    account_key = connection_parts.get("AccountKey", "")

    if not account_name or not account_key:
        return blob_client.url

    sas_token = generate_blob_sas(
        account_name=account_name,
        account_key=account_key,
        container_name=RFQ_FILES_CONTAINER,
        blob_name=blob_name,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650),
    )
    return f"{blob_client.url}?{sas_token}" if sas_token else blob_client.url


def _safe_upload_filename(filename: str | None) -> str:
    return os.path.basename(filename or "attachment") or "attachment"


def _extract_local_stored_name(file_meta: dict) -> str:
    path = str(file_meta.get("path") or file_meta.get("download_url") or "")
    if "/api/rfq/download/" not in path:
        return ""
    return path.rsplit("/", 1)[-1]


def _delete_legacy_local_file(file_meta: dict) -> None:
    stored_name = _extract_local_stored_name(file_meta)
    if not stored_name:
        return
    file_path = os.path.join("uploads", stored_name)
    if os.path.exists(file_path):
        os.remove(file_path)


def _delete_azure_blob(file_meta: dict) -> None:
    blob_name = str(file_meta.get("blob_name") or "").strip()
    if not blob_name or not settings.azure_connection_string:
        return
    try:
        _get_rfq_files_container_client().delete_blob(blob_name)
    except ResourceNotFoundError:
        pass


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


def _validation_action_timestamp(rfq: Rfq) -> datetime.datetime | None:
    return rfq.approved_at or rfq.rejected_at


def _assert_terminal_status_allowed(rfq: Rfq, target_phase: RfqPhase, target_sub_status: RfqSubStatus) -> None:
    if (
        target_phase == RfqPhase.CLOSED
        and target_sub_status == RfqSubStatus.LOST
        and rfq.phase in {RfqPhase.RFQ, RfqPhase.COSTING}
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQs rejected during the RFQ or COSTING phases must close as "
                "CANCELED, not LOST."
            ),
        )


def _can_view_rfq(current_user: User, rfq: Rfq) -> bool:
    return (
        current_user.role == UserRole.OWNER
        or rfq.created_by_email == current_user.email
        or rfq.zone_manager_email == current_user.email
    )


def _assert_can_view_rfq(current_user: User, rfq: Rfq) -> None:
    if not _can_view_rfq(current_user, rfq):
        raise HTTPException(status_code=403, detail="Not authorized to access this RFQ.")


def _rfq_query():
    return select(Rfq).options(selectinload(Rfq.potential))


async def _get_rfq_or_404(db: AsyncSession, rfq_id: str) -> Rfq:
    result = await db.execute(_rfq_query().where(Rfq.rfq_id == rfq_id))
    rfq = result.scalar_one_or_none()
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found.")
    return rfq


def _build_discussion_message_out(
    message: DiscussionMessage,
    author: User,
) -> DiscussionMessageOut:
    return DiscussionMessageOut(
        id=message.id,
        rfq_id=message.rfq_id,
        phase=message.phase,
        message=message.message,
        created_at=message.created_at,
        user_id=author.user_id,
        author_name=author.full_name,
        author_email=author.email,
        author_role=author.role,
    )


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
    if rfq.sub_status == RfqSubStatus.POTENTIAL:
        return next_data
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
    validator_role: str = "Validator",
) -> None:
    frontend_url = settings.frontend_url
    rfq_link = f"{frontend_url}/rfqs/new?id={rfq_id}"

    msg = EmailMessage()
    msg["Subject"] = f"Action Required: {validator_role} Review for RFQ {systematic_rfq_id}"
    msg["From"] = SMTP_FROM
    msg["To"] = zone_manager_email

    text_body = f"""Hello,

A new RFQ ({systematic_rfq_id}) for the {acronym} product line has been submitted.
It requires your validation as the {validator_role} in order to proceed to the Costing phase.
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
          <h2 style="color: #1a365d; margin-top: 0;">{validator_role} Review Required</h2>
          <p>Hello,</p>
          <p>A new RFQ <strong>({systematic_rfq_id})</strong> for the <strong>{acronym}</strong> product line has been submitted. It requires your validation as the <strong>{validator_role}</strong> in order to proceed to the Costing phase.</p>

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


async def _submit_rfq_for_validation_internal(
    *,
    rfq: Rfq,
    db: AsyncSession,
    current_user: User,
    send_email: bool = True,
) -> dict[str, str | bool]:
    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to submit this RFQ.")

    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only RFQs in RFQ/NEW_RFQ can be submitted for validation. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    extracted_data = dict(rfq.rfq_data or {})
    acronym = (extracted_data.get("product_line_acronym") or "").strip()
    revision = str(extracted_data.get("revision_level") or "00").strip() or "00"
    zone_manager_email = (
        extracted_data.get("zone_manager_email") or extracted_data.get("validator_email") or ""
    ).strip()
    validator_role = str(extracted_data.get("validator_role") or "Validator").strip() or "Validator"

    if not acronym:
        raise HTTPException(
            status_code=400,
            detail="Product line acronym is missing. Cannot submit this RFQ.",
        )
    if not zone_manager_email:
        raise HTTPException(
            status_code=400,
            detail="Validator email is missing. Cannot submit this RFQ.",
        )

    extracted_data["product_line_acronym"] = acronym
    extracted_data["zone_manager_email"] = zone_manager_email
    extracted_data["validator_role"] = validator_role
    extracted_data.pop("validator_email", None)
    if not extracted_data.get("systematic_rfq_id"):
        extracted_data["systematic_rfq_id"] = await _generate_systematic_rfq_id(
            db, acronym, revision
        )
    systematic_rfq_id = str(extracted_data["systematic_rfq_id"])
    rfq.rfq_data = extracted_data
    rfq.zone_manager_email = zone_manager_email
    rfq.product_line_acronym = acronym
    rfq.approved_at = None
    rfq.rejected_at = None
    _set_phase_sub_status(rfq, RfqPhase.RFQ, RfqSubStatus.PENDING_FOR_VALIDATION)

    await log_action(
        db,
        rfq.rfq_id,
        (
            "RFQ submitted for validation -> "
            f"{RfqPhase.RFQ.value}/{RfqSubStatus.PENDING_FOR_VALIDATION.value}"
        ),
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)

    if send_email:
        _send_validation_email(
            zone_manager_email,
            systematic_rfq_id,
            acronym,
            rfq.rfq_id,
            validator_role=validator_role,
        )

    return {
        "message": "RFQ submitted for validation.",
        "systematic_rfq_id": systematic_rfq_id,
        "phase": rfq.phase.value,
        "sub_status": rfq.sub_status.value,
        "zone_manager_email": zone_manager_email,
        "validator_role": validator_role,
        "email_sent": send_email,
    }


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
    rfq_data = rfq_data_payload_to_dict(request_body.rfq_data)
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
    if initial_sub_status == RfqSubStatus.POTENTIAL:
        rfq.potential = Potential(chat_history=[])

    await db.commit()
    return await _get_rfq_or_404(db, rfq.rfq_id)


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

    incoming_data = rfq_data_payload_to_dict(body.rfq_data)
    next_data = dict(rfq.rfq_data or {})
    next_data.update(incoming_data)
    rfq.rfq_data = next_data

    if "product_line_acronym" in incoming_data:
        rfq.product_line_acronym = incoming_data.get("product_line_acronym")
    if "zone_manager_email" in incoming_data or "validator_email" in incoming_data:
        rfq.zone_manager_email = (
            incoming_data.get("zone_manager_email")
            or incoming_data.get("validator_email")
        )

    rfq.rfq_data = await _maybe_assign_systematic_rfq_id(db, rfq, next_data)

    await db.commit()
    return await _get_rfq_or_404(db, rfq_id)


@router.post("/{rfq_id}/proceed-to-rfq", response_model=RfqOut)
async def proceed_to_rfq(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to proceed with this RFQ.")

    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.POTENTIAL):
        raise HTTPException(
            status_code=409,
            detail="This opportunity is no longer in the Potential phase.",
        )

    potential = rfq.potential
    if potential is None:
        raise HTTPException(status_code=400, detail="Potential data is missing for this RFQ.")

    missing_fields = get_missing_potential_shared_fields(potential)
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Complete the Potential shared fields before proceeding.",
                "missing_fields": missing_fields,
            },
        )

    rfq.rfq_data = sync_potential_to_rfq_data(potential, rfq.rfq_data)
    rfq.sub_status = RfqSubStatus.NEW_RFQ

    await log_action(db, rfq_id, "Potential promoted to formal RFQ", current_user.email)
    await db.commit()
    return await _get_rfq_or_404(db, rfq_id)


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

    safe_name = _safe_upload_filename(file.filename)
    file_id = str(uuid.uuid4())
    blob_name = f"{rfq_id}/{file_id}-{safe_name}"

    try:
        await file.seek(0)
        container_client = _get_rfq_files_container_client()
        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(
            file.file,
            overwrite=False,
            content_settings=ContentSettings(
                content_type=file.content_type or "application/octet-stream"
            ),
        )
        blob_access_url = _build_blob_access_url(blob_name)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to upload file to Azure Blob Storage: {exc}",
        ) from exc

    file_meta = {
        "id": file_id,
        "name": safe_name,
        "filename": safe_name,
        "path": blob_access_url,
        "url": blob_access_url,
        "download_url": blob_access_url,
        "blob_url": blob_client.url,
        "blob_name": blob_name,
        "content_type": file.content_type or "application/octet-stream",
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
        "file_path": file_meta["url"],
        "file": file_meta,
    }


@router.delete("/{rfq_id}/files/{file_id}")
async def delete_rfq_file(
    rfq_id: str,
    file_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to delete files from this RFQ.")

    extracted_data = dict(rfq.rfq_data or {})
    existing_files = list(extracted_data.get("rfq_files") or [])

    target_index = next(
        (
            index
            for index, entry in enumerate(existing_files)
            if str(entry.get("id") or entry.get("blob_name") or entry.get("filename") or "") == file_id
        ),
        -1,
    )

    if target_index < 0:
        raise HTTPException(status_code=404, detail="File not found.")

    removed_file = existing_files.pop(target_index)
    _delete_azure_blob(removed_file)
    _delete_legacy_local_file(removed_file)

    extracted_data["rfq_files"] = existing_files
    if existing_files:
        latest_file = existing_files[-1]
        extracted_data["rfq_file_path"] = (
            latest_file.get("url")
            or latest_file.get("download_url")
            or latest_file.get("path")
        )
    else:
        extracted_data.pop("rfq_file_path", None)

    rfq.rfq_data = extracted_data
    await db.commit()
    await db.refresh(rfq)

    return {"message": "File deleted successfully"}


@router.delete("/{rfq_id}/files")
async def delete_rfq_file_by_name(
    rfq_id: str,
    body: dict | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    filename = str((body or {}).get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required.")

    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to delete files from this RFQ.")

    extracted_data = dict(rfq.rfq_data or {})
    existing_files = list(extracted_data.get("rfq_files") or [])

    target_index = next(
        (
            index
            for index, entry in enumerate(existing_files)
            if str(entry.get("filename") or entry.get("name") or "").strip() == filename
        ),
        -1,
    )

    if target_index < 0:
        raise HTTPException(status_code=404, detail="File not found.")

    removed_file = existing_files.pop(target_index)
    _delete_azure_blob(removed_file)
    _delete_legacy_local_file(removed_file)

    extracted_data["rfq_files"] = existing_files
    if existing_files:
        latest_file = existing_files[-1]
        extracted_data["rfq_file_path"] = (
            latest_file.get("url")
            or latest_file.get("download_url")
            or latest_file.get("path")
        )
    else:
        extracted_data.pop("rfq_file_path", None)

    rfq.rfq_data = extracted_data
    await db.commit()
    await db.refresh(rfq)

    return {"message": "File deleted successfully"}


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
    query = _rfq_query().order_by(Rfq.updated_at.desc(), Rfq.created_at.desc())

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


@router.get("/{rfq_id}/discussion", response_model=list[DiscussionMessageOut])
async def get_rfq_discussion(
    rfq_id: str,
    phase: RfqSubStatus,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_view_rfq(current_user, rfq)

    result = await db.execute(
        select(DiscussionMessage, User)
        .join(User, DiscussionMessage.user_id == User.user_id)
        .where(
            DiscussionMessage.rfq_id == rfq_id,
            DiscussionMessage.phase == phase,
        )
        .order_by(DiscussionMessage.created_at.asc(), DiscussionMessage.id.asc())
    )
    return [
        _build_discussion_message_out(message, author)
        for message, author in result.all()
    ]


@router.post("/{rfq_id}/discussion", response_model=DiscussionMessageOut, status_code=201)
async def create_rfq_discussion_message(
    rfq_id: str,
    body: DiscussionMessageCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_view_rfq(current_user, rfq)

    message = DiscussionMessage(
        rfq_id=rfq_id,
        user_id=current_user.user_id,
        phase=body.phase,
        message=body.message,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    return _build_discussion_message_out(message, current_user)


@router.get("/{rfq_id}/costing-template")
async def download_costing_template(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_view_rfq(current_user, rfq)

    if rfq.phase == RfqPhase.RFQ and rfq.approved_at is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "The costing template is only available after the RFQ has been approved "
                "to the costing phase."
            ),
        )

    filename = build_costing_template_filename(rfq)
    try:
        document_pdf = render_costing_template_pdf(rfq)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to generate the costing template PDF. {exc}",
        ) from exc

    return Response(
        content=document_pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{rfq_id}/submit")
async def submit_rfq_for_validation(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    return await _submit_rfq_for_validation_internal(
        rfq=rfq,
        db=db,
        current_user=current_user,
        send_email=True,
    )


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
    _assert_terminal_status_allowed(rfq, body.phase, body.sub_status)
    _ensure_valid_phase_sub_status(body.phase, body.sub_status)
    _set_phase_sub_status(rfq, body.phase, body.sub_status)

    await log_action(
        db,
        rfq_id,
        f"Status updated to {body.phase.value}/{body.sub_status.value}",
        current_user.email,
    )
    await db.commit()
    return await _get_rfq_or_404(db, rfq_id)


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
    return await _get_rfq_or_404(db, rfq_id)


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
    current_user: User = Depends(
        require_role(UserRole.COMMERCIAL, UserRole.ZONE_MANAGER, UserRole.OWNER)
    ),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    assigned_validator_email = str(rfq.zone_manager_email or "").strip()
    current_user_email = str(current_user.email or "").strip()
    is_assigned_validator = (
        bool(assigned_validator_email)
        and assigned_validator_email.casefold() == current_user_email.casefold()
    )

    if current_user.role != UserRole.OWNER and not is_assigned_validator:
        raise HTTPException(
            status_code=403,
            detail="You are not assigned as the Validator for this RFQ.",
        )

    if _validation_action_timestamp(rfq):
        raise HTTPException(
            status_code=400,
            detail="A validation action has already been recorded for this RFQ.",
        )

    if (rfq.phase, rfq.sub_status) != (
        RfqPhase.RFQ,
        RfqSubStatus.PENDING_FOR_VALIDATION,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQ must be in RFQ/PENDING_FOR_VALIDATION before it can be validated. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    if body.approved:
        _set_phase_sub_status(rfq, RfqPhase.COSTING, RfqSubStatus.FEASIBILITY)
        rfq.approved_at = datetime.datetime.now(datetime.timezone.utc)
        rfq.rejected_at = None
        rfq.rejection_reason = None
        await log_action(
            db,
            rfq_id,
            f"Validator approved -> {RfqPhase.COSTING.value}/{RfqSubStatus.FEASIBILITY.value}",
            current_user.email,
        )
    else:
        _set_phase_sub_status(rfq, RfqPhase.CLOSED, RfqSubStatus.CANCELED)
        rfq.approved_at = None
        rfq.rejected_at = datetime.datetime.now(datetime.timezone.utc)
        rfq.rejection_reason = body.rejection_reason
        await log_action(
            db,
            rfq_id,
            f"Validator rejected -> {RfqPhase.CLOSED.value}/{RfqSubStatus.CANCELED.value}: {body.rejection_reason}",
            current_user.email,
        )

    await db.commit()
    return await _get_rfq_or_404(db, rfq_id)


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

    if body.scope:
        await log_action(
            db,
            rfq_id,
            "Costing review approved",
            current_user.email,
        )
    else:
        _set_phase_sub_status(rfq, RfqPhase.CLOSED, RfqSubStatus.CANCELED)
        rfq.rejection_reason = body.rejection_reason
        await log_action(
            db,
            rfq_id,
            f"Costing review rejected -> {RfqPhase.CLOSED.value}/{RfqSubStatus.CANCELED.value}: {body.rejection_reason}",
            current_user.email,
        )

    await db.commit()
    return await _get_rfq_or_404(db, rfq_id)


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
    return await _get_rfq_or_404(db, rfq_id)
