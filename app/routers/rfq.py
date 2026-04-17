import datetime
import os
import uuid
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database_assembly import sync_rfq_to_assembly
from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.audit_log import AuditLog
from app.models.discussion import DiscussionMessage
from app.models.potential import Potential
from app.models.rfq import ALLOWED_TRANSITIONS, Rfq, RfqPhase, RfqSubStatus, VALID_PHASE_SUBSTATUS
from app.models.user import User, UserRole
from app.schemas.discussion import (
    CostingMessageCreateRequest,
    DiscussionMessageCreateRequest,
    DiscussionMessageOut,
)
from app.schemas.rfq import (
    AdvanceStatusRequest,
    AuditLogOut,
    AutopsyRequest,
    CostingReviewRequest,
    PhaseStatusUpdateRequest,
    RequestRevisionRequest,
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
from app.utils import emails

router = APIRouter(prefix="/api/rfq", tags=["rfq"])

TERMINAL_SUBSTATUSES = {RfqSubStatus.LOST, RfqSubStatus.CANCELED}
RFQ_FILES_CONTAINER = "rfq-files"
COSTING_DISCUSSION_PHASES = {RfqSubStatus.FEASIBILITY, RfqSubStatus.PRICING}
PRODUCT_LINE_MATRIX = {
    "Chokes": {"email": "mohamedlaith.benmabrouk@avocarbon.com", "code": "CHO"},
    "Brushes": {"email": "mohamedlaith.benmabrouk@avocarbon.com", "code": "BRU"},
    "Seals": {"email": "mohamedlaith.benmabrouk@avocarbon.com", "code": "SEA"},
    "Assembly": {"email": "mohamedlaith.benmabrouk@avocarbon.com", "code": "ASS"},
    "Advanced material": {"email": "mohamedlaith.benmabrouk@avocarbon.com", "code": "ADM"},
}
COSTING_FILE_STATUS_PENDING = "PENDING"
COSTING_FILE_STATUS_UPLOADED = "UPLOADED"
COSTING_FILE_STATUS_NA = "NA"


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


def _assert_terminal_status_allowed(rfq: Rfq, target_sub_status: RfqSubStatus) -> None:
    if (
        target_sub_status == RfqSubStatus.LOST
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
        or (current_user.role == UserRole.COSTING_TEAM and rfq.phase == RfqPhase.COSTING)
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
        recipient_email=message.recipient_email,
        created_at=message.created_at,
        user_id=author.user_id,
        author_name=author.full_name,
        author_email=author.email,
        author_role=author.role,
    )


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _normalize_product_line_key(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _resolve_product_line_route(rfq: Rfq) -> dict[str, str] | None:
    rfq_data = dict(rfq.rfq_data or {})
    candidates = [
        rfq_data.get("product_name"),
        rfq_data.get("product_line"),
        rfq_data.get("productLine"),
        rfq_data.get("product_line_name"),
        rfq.product_line_acronym,
        rfq_data.get("product_line_acronym"),
    ]
    normalized_lookup: dict[str, dict[str, str]] = {}
    for product_line, entry in PRODUCT_LINE_MATRIX.items():
        normalized_lookup[_normalize_product_line_key(product_line)] = {
            **entry,
            "product_line": product_line,
        }
        normalized_lookup[_normalize_product_line_key(entry.get("code"))] = {
            **entry,
            "product_line": product_line,
        }

    for candidate in candidates:
        resolved = normalized_lookup.get(_normalize_product_line_key(candidate))
        if resolved:
            return resolved
    return None


def _default_costing_file_state() -> dict[str, str | None]:
    return {
        "file_status": COSTING_FILE_STATUS_PENDING,
        "file_note": None,
        "action_by": None,
        "action_at": None,
        "file": None,
    }


def _effective_costing_file_state(rfq: Rfq) -> dict:
    state = dict(rfq.costing_file_state or {})
    status = str(state.get("file_status") or "").strip().upper()
    if status in {
        COSTING_FILE_STATUS_PENDING,
        COSTING_FILE_STATUS_UPLOADED,
        COSTING_FILE_STATUS_NA,
    }:
        return state

    legacy_files = list(rfq.costing_files or [])
    if legacy_files:
        latest_file = legacy_files[-1]
        return {
            "file_status": COSTING_FILE_STATUS_UPLOADED,
            "file_note": state.get("file_note"),
            "action_by": latest_file.get("uploaded_by") or latest_file.get("owner"),
            "action_at": latest_file.get("uploaded_at") or latest_file.get("updated_at"),
            "file": latest_file,
        }

    return _default_costing_file_state()


def _costing_file_state_allows_progression(rfq: Rfq) -> bool:
    status = str(_effective_costing_file_state(rfq).get("file_status") or "").upper()
    return status in {COSTING_FILE_STATUS_UPLOADED, COSTING_FILE_STATUS_NA}


def _ensure_costing_file_state_initialized(rfq: Rfq) -> None:
    if not rfq.costing_file_state:
        rfq.costing_file_state = _default_costing_file_state()


async def _has_costing_review_approval(db: AsyncSession, rfq_id: str) -> bool:
    result = await db.execute(
        select(AuditLog.log_id)
        .where(
            AuditLog.rfq_id == rfq_id,
            AuditLog.action == "Costing review approved",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _build_rfq_link(rfq_id: str) -> str:
    frontend_url = str(settings.frontend_url or "").rstrip("/")
    return f"{frontend_url}/rfqs/new?id={rfq_id}" if frontend_url else rfq_id

async def _upload_costing_action_file(
    *,
    rfq_id: str,
    file: UploadFile,
    current_user_email: str,
) -> dict[str, str]:
    safe_name = _safe_upload_filename(file.filename)
    file_id = str(uuid.uuid4())
    blob_name = f"{rfq_id}/costing/{file_id}-{safe_name}"

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
            detail=f"Unable to upload costing file to Azure Blob Storage: {exc}",
        ) from exc

    return {
        "id": file_id,
        "name": safe_name,
        "filename": safe_name,
        "path": blob_access_url,
        "url": blob_access_url,
        "download_url": blob_access_url,
        "blob_url": blob_client.url,
        "blob_name": blob_name,
        "content_type": file.content_type or "application/octet-stream",
        "uploaded_by": current_user_email,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _allowed_transitions_for(rfq: Rfq) -> set[tuple[RfqPhase, RfqSubStatus]]:
    allowed = set(ALLOWED_TRANSITIONS.get((rfq.phase, rfq.sub_status), set()))

    # Business clarification: a "mission not accepted" outcome must close the RFQ
    # immediately with LOST or CANCELED plus autopsy notes.
    if (rfq.phase, rfq.sub_status) == (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED):
        allowed.update(
            {
                (RfqPhase.PO, RfqSubStatus.LOST),
                (RfqPhase.PO, RfqSubStatus.CANCELED),
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

    email_sent = False
    if send_email:
        email_sent = emails.send_validation_email(
            zone_manager_email,
            systematic_rfq_id,
            acronym,
            _build_rfq_link(rfq.rfq_id),
            validator_role=validator_role,
        )

    return {
        "message": "RFQ submitted for validation.",
        "systematic_rfq_id": systematic_rfq_id,
        "phase": rfq.phase.value,
        "sub_status": rfq.sub_status.value,
        "zone_manager_email": zone_manager_email,
        "validator_role": validator_role,
        "email_sent": email_sent,
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


@router.get("/{rfq_id}/costing-messages", response_model=list[DiscussionMessageOut])
async def get_costing_messages(
    rfq_id: str,
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
            DiscussionMessage.phase.in_(tuple(COSTING_DISCUSSION_PHASES)),
        )
        .order_by(DiscussionMessage.created_at.asc(), DiscussionMessage.id.asc())
    )
    return [
        _build_discussion_message_out(message, author)
        for message, author in result.all()
    ]


@router.post("/{rfq_id}/costing-messages", response_model=DiscussionMessageOut, status_code=201)
async def create_costing_message(
    rfq_id: str,
    body: CostingMessageCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    if not _can_view_rfq(current_user, rfq) and current_user.role != UserRole.COSTING_TEAM:
        raise HTTPException(status_code=403, detail="Not authorized to access this RFQ.")

    if (rfq.phase, rfq.sub_status) not in {
        (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY),
        (RfqPhase.COSTING, RfqSubStatus.PRICING),
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "Costing messages are only available during the costing phase. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    message = DiscussionMessage(
        rfq_id=rfq_id,
        user_id=current_user.user_id,
        phase=rfq.sub_status,
        message=body.message,
        recipient_email=body.recipient_email,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    rfq_data = dict(rfq.rfq_data or {})
    emails.send_costing_message_email(
        body.recipient_email,
        str(rfq_data.get("systematic_rfq_id") or ""),
        current_user.email,
        body.message,
        _build_rfq_link(rfq_id),
    )

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


@router.post("/{rfq_id}/request-revision", response_model=RfqOut)
async def request_revision(
    rfq_id: str,
    body: RequestRevisionRequest,
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

    if (rfq.phase, rfq.sub_status) != (
        RfqPhase.RFQ,
        RfqSubStatus.PENDING_FOR_VALIDATION,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQ must be in RFQ/PENDING_FOR_VALIDATION before requesting a revision. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=400, detail="comment is required.")

    rfq.revision_notes = comment
    _set_phase_sub_status(rfq, RfqPhase.RFQ, RfqSubStatus.REVISION_REQUESTED)

    await log_action(
        db,
        rfq_id,
        f"Revision requested -> {RfqPhase.RFQ.value}/{RfqSubStatus.REVISION_REQUESTED.value}: {comment}",
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)

    rfq_data = dict(rfq.rfq_data or {})
    emails.send_revision_request_email(
        rfq.created_by_email,
        str(rfq_data.get("systematic_rfq_id") or ""),
        comment,
        _build_rfq_link(rfq_id),
    )

    return await _get_rfq_or_404(db, rfq_id)


@router.post("/{rfq_id}/submit-revision", response_model=RfqOut)
async def submit_revision(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role != UserRole.OWNER and rfq.created_by_email != current_user.email:
        raise HTTPException(status_code=403, detail="Not authorized to submit this revision.")

    if (rfq.phase, rfq.sub_status) != (
        RfqPhase.RFQ,
        RfqSubStatus.REVISION_REQUESTED,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "RFQ must be in RFQ/REVISION_REQUESTED before submitting updates. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    rfq.revision_notes = None
    _set_phase_sub_status(rfq, RfqPhase.RFQ, RfqSubStatus.PENDING_FOR_VALIDATION)

    await log_action(
        db,
        rfq_id,
        f"Revision submitted -> {RfqPhase.RFQ.value}/{RfqSubStatus.PENDING_FOR_VALIDATION.value}",
        current_user.email,
    )
    await db.commit()
    return await _get_rfq_or_404(db, rfq_id)


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
    effective_phase = body.phase
    if body.sub_status in TERMINAL_SUBSTATUSES:
        effective_phase = rfq.phase
        _assert_terminal_status_allowed(rfq, body.sub_status)
    _ensure_valid_phase_sub_status(effective_phase, body.sub_status)
    _set_phase_sub_status(rfq, effective_phase, body.sub_status)

    await log_action(
        db,
        rfq_id,
        f"Status updated to {effective_phase.value}/{body.sub_status.value}",
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
        _ensure_costing_file_state_initialized(rfq)
        await log_action(
            db,
            rfq_id,
            f"Validator approved -> {RfqPhase.COSTING.value}/{RfqSubStatus.FEASIBILITY.value}",
            current_user.email,
        )
    else:
        _set_phase_sub_status(rfq, rfq.phase, RfqSubStatus.CANCELED)
        rfq.approved_at = None
        rfq.rejected_at = datetime.datetime.now(datetime.timezone.utc)
        rfq.rejection_reason = body.rejection_reason
        await log_action(
            db,
            rfq_id,
            f"Validator rejected -> {rfq.phase.value}/{RfqSubStatus.CANCELED.value}: {body.rejection_reason}",
            current_user.email,
        )

    await db.commit()
    refreshed_rfq = await _get_rfq_or_404(db, rfq_id)

    if body.approved:
        route_entry = _resolve_product_line_route(refreshed_rfq)
        if route_entry:
            refreshed_data = dict(refreshed_rfq.rfq_data or {})
            emails.send_costing_entry_email(
                str(route_entry.get("email") or ""),
                str(route_entry.get("product_line") or ""),
                str(route_entry.get("code") or ""),
                str(refreshed_data.get("systematic_rfq_id") or ""),
                _build_rfq_link(refreshed_rfq.rfq_id),
            )

    return refreshed_rfq


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
        _set_phase_sub_status(rfq, rfq.phase, RfqSubStatus.CANCELED)
        rfq.rejection_reason = body.rejection_reason
        await log_action(
            db,
            rfq_id,
            f"Costing review rejected -> {rfq.phase.value}/{RfqSubStatus.CANCELED.value}: {body.rejection_reason}",
            current_user.email,
        )

    await db.commit()
    refreshed_rfq = await _get_rfq_or_404(db, rfq_id)

    cc_email = str(refreshed_rfq.zone_manager_email or "").strip() or None
    if _normalize_email(cc_email) == _normalize_email(refreshed_rfq.created_by_email):
        cc_email = None
    refreshed_data = dict(refreshed_rfq.rfq_data or {})
    systematic_rfq_id = str(refreshed_data.get("systematic_rfq_id") or "")
    rfq_link = _build_rfq_link(refreshed_rfq.rfq_id)
    emails.send_costing_reception_results_email(
        refreshed_rfq.created_by_email,
        cc_email,
        current_user.email,
        systematic_rfq_id,
        rfq_link,
        is_approved=body.scope,
        rejection_reason=body.rejection_reason,
    )

    if body.scope:
        route_entry = _resolve_product_line_route(refreshed_rfq)
        if route_entry:
            emails.send_costing_handoff_email(
                str(route_entry.get("email") or ""),
                str(route_entry.get("product_line") or ""),
                str(route_entry.get("code") or ""),
                systematic_rfq_id,
                rfq_link,
            )
        if str(refreshed_rfq.product_line_acronym or "").upper() == "ASS":
            await sync_rfq_to_assembly(refreshed_rfq)

    return refreshed_rfq


@router.post("/{rfq_id}/costing-file-action", response_model=RfqOut)
async def submit_costing_file_action(
    rfq_id: str,
    action: str = Form(...),
    note: str = Form(...),
    file: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)

    if (rfq.phase, rfq.sub_status) != (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY):
        raise HTTPException(
            status_code=400,
            detail=(
                "Costing file actions are only allowed during COSTING/FEASIBILITY. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    normalized_action = str(action or "").strip().upper()
    if normalized_action not in {COSTING_FILE_STATUS_UPLOADED, COSTING_FILE_STATUS_NA}:
        raise HTTPException(
            status_code=400,
            detail="action must be either 'UPLOADED' or 'NA'.",
        )

    trimmed_note = str(note or "").strip()
    if not trimmed_note:
        raise HTTPException(status_code=400, detail="note is required.")

    if normalized_action == COSTING_FILE_STATUS_UPLOADED and file is None:
        raise HTTPException(
            status_code=400,
            detail="file is required when action is 'UPLOADED'.",
        )
    if normalized_action == COSTING_FILE_STATUS_NA and file is not None:
        raise HTTPException(
            status_code=400,
            detail="file must not be provided when action is 'NA'.",
        )

    file_meta = None
    if normalized_action == COSTING_FILE_STATUS_UPLOADED and file is not None:
        file_meta = await _upload_costing_action_file(
            rfq_id=rfq_id,
            file=file,
            current_user_email=current_user.email,
        )
        rfq.costing_files = list(rfq.costing_files or []) + [file_meta]

    rfq.costing_file_state = {
        "file_status": normalized_action,
        "file_note": trimmed_note,
        "action_by": current_user.email,
        "action_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "file": file_meta,
    }

    action_label = "Not applicable noted" if normalized_action == COSTING_FILE_STATUS_NA else "Costing file uploaded"
    await log_action(
        db,
        rfq_id,
        f"{action_label}: {trimmed_note}",
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
                "Use the current phase with LOST or CANCELED and autopsy_notes instead."
            ),
        )

    # For terminal sub-statuses (LOST/CANCELED), override the target phase
    # to the current phase so the RFQ stays where it was canceled.
    effective_phase = body.target_phase
    if body.target_sub_status in TERMINAL_SUBSTATUSES:
        effective_phase = rfq.phase
        _assert_terminal_status_allowed(rfq, body.target_sub_status)

    target_state = (effective_phase, body.target_sub_status)
    allowed = _allowed_transitions_for(rfq)
    if target_state not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"Cannot advance from {rfq.phase.value}/{rfq.sub_status.value} "
                    f"to {effective_phase.value}/{body.target_sub_status.value}."
                ),
                "allowed": [
                    {"phase": phase.value, "sub_status": sub_status.value}
                    for phase, sub_status in allowed
                ],
            },
        )

    if target_state == (RfqPhase.COSTING, RfqSubStatus.PRICING):
        if not await _has_costing_review_approval(db, rfq_id):
            raise HTTPException(
                status_code=400,
                detail="A costing reception approval is required before moving to pricing.",
            )
        if not _costing_file_state_allows_progression(rfq):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Complete the costing file action first by uploading the feasibility file "
                    "or marking it as not applicable."
                ),
            )

    _set_phase_sub_status(rfq, effective_phase, body.target_sub_status)

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
