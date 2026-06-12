import datetime
import os
import uuid
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database_assembly import sync_rfq_to_assembly
from app.database import get_db, get_db3
from app.middleware.auth import get_current_user, require_role
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.discussion import DiscussionMessage
from app.models.notification_log import NotificationLog
from app.models.potential import Potential
from app.models.product_line_routing import ProductLineRoutingRole
from app.models.rfq import (
    ALLOWED_TRANSITIONS,
    Rfq,
    RfqDocumentType,
    RfqPhase,
    RfqSubStatus,
    VALID_PHASE_SUBSTATUS,
)
from app.models.user import User, UserRole
from app.models.validation_matrix import ValidationMatrix
from app.schemas.discussion import (
    CostingMessageCreateRequest,
    DiscussionMessageCreateRequest,
    DiscussionMessageOut,
)
from app.schemas.rfq import (
    AdvanceStatusRequest,
    AuditLogOut,
    CostingValidationRequest,
    AutopsyRequest,
    CostingReviewRequest,
    NotificationLogOut,
    PhaseStatusUpdateRequest,
    ProceedToFormalRequest,
    RequestRevisionRequest,
    RfqCreateRequest,
    RfqDataUpdateRequest,
    RfqFxRateOut,
    RfqOut,
    ValidateRfqRequest,
    get_conflicting_product_currencies,
    get_incomplete_product_fields,
    normalize_rfq_data_products,
    rfq_data_payload_to_dict,
)
from app.services.audit import log_action
from app.services.costing_template import (
    build_costing_template_filename,
    render_costing_template_pdf,
)
from app.services.offer_template import (
    build_offer_preparation_filename,
    render_offer_preparation_docx,
    render_offer_preparation_preview_html,
)
from app.services.potential import (
    get_missing_potential_shared_fields,
    sync_potential_to_rfq_data,
)
from app.services.notifications import (
    EMAIL_BOM_READY,
    EMAIL_COSTING_APPROVED,
    EMAIL_COSTING_ENTRY,
    EMAIL_COSTING_HANDOFF,
    EMAIL_COSTING_MESSAGE,
    EMAIL_COSTING_RECEPTION_RESULT,
    EMAIL_COSTING_REJECTED,
    EMAIL_FEASIBILITY_RESULT,
    EMAIL_PRICING_READY,
    EMAIL_REVISION_REQUEST,
    EMAIL_RFI_COMPLETED,
    EMAIL_VALIDATION_REQUEST,
    record_notification_sent,
)
from app.services.offer_preparation_store import get_offer_preparation_data_snapshot
from app.services.routing import (
    get_assigned_product_line_acronyms,
    resolve_product_line_role_assignment,
    resolve_product_line_role_email,
)
from app.utils import emails
from app.utils.currency import get_eur_exchange_rate

router = APIRouter(prefix="/api/rfq", tags=["rfq"])

TERMINAL_SUBSTATUSES = {RfqSubStatus.LOST, RfqSubStatus.CANCELED}
RFI_BLOCKED_FORWARD_PHASES = {RfqPhase.OFFER, RfqPhase.PO, RfqPhase.PROTOTYPE}
RFQ_FILES_CONTAINER = "rfq-files"
COSTING_DISCUSSION_PHASES = {RfqSubStatus.FEASIBILITY, RfqSubStatus.PRICING}
COSTING_FILE_STATUS_PENDING = "PENDING"
COSTING_FILE_STATUS_UPLOADED = "UPLOADED"
COSTING_FILE_STATUS_NA = "NA"
FEASIBILITY_STATUS_FEASIBLE = "FEASIBLE"
FEASIBILITY_STATUS_FEASIBLE_UNDER_CONDITION = "FEASIBLE_UNDER_CONDITION"
FEASIBILITY_STATUS_NOT_FEASIBLE = "NOT_FEASIBLE"
FEASIBILITY_STATUSES = {
    FEASIBILITY_STATUS_FEASIBLE,
    FEASIBILITY_STATUS_FEASIBLE_UNDER_CONDITION,
    FEASIBILITY_STATUS_NOT_FEASIBLE,
}
PRICING_COSTING_FILE_ROLES = {"PRICING_BOM", "PRICING_FINAL_PRICE"}
PRICING_WORKFLOW_STATE_WAITING_BOM = "WAITING_BOM"
PRICING_WORKFLOW_STATE_BOM_UPLOADED = "BOM_UPLOADED"
PRICING_WORKFLOW_STATE_PRICING_UPLOADED = "PRICING_UPLOADED"
PRICING_WORKFLOW_STATE_APPROVED = "APPROVED"
PRICING_WORKFLOW_STATE_REJECTED = "REJECTED"
PRICING_WORKFLOW_STATES = {
    PRICING_WORKFLOW_STATE_WAITING_BOM,
    PRICING_WORKFLOW_STATE_BOM_UPLOADED,
    PRICING_WORKFLOW_STATE_PRICING_UPLOADED,
    PRICING_WORKFLOW_STATE_APPROVED,
    PRICING_WORKFLOW_STATE_REJECTED,
}


def _document_type_value(rfq: Rfq) -> str:
    document_type = rfq.document_type or RfqDocumentType.RFQ
    value = document_type.value if isinstance(document_type, RfqDocumentType) else str(document_type)
    return value.strip().upper()


def _is_rfi(rfq: Rfq) -> bool:
    return _document_type_value(rfq) == RfqDocumentType.RFI.value


def _is_potential(rfq: Rfq) -> bool:
    return _document_type_value(rfq) == RfqDocumentType.POTENTIAL.value


def _parse_document_type_filters(values: list[str] | None) -> list[RfqDocumentType]:
    document_types: list[RfqDocumentType] = []
    for raw_value in values or []:
        for token in str(raw_value or "").split(","):
            normalized = token.strip().upper()
            if not normalized:
                continue
            try:
                document_type = RfqDocumentType(normalized)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid document_type: {token.strip()}",
                ) from exc
            if document_type not in document_types:
                document_types.append(document_type)
    return document_types


def _assert_document_type_allows_target(
    rfq: Rfq,
    target_phase: RfqPhase,
    target_sub_status: RfqSubStatus,
) -> None:
    if target_sub_status == RfqSubStatus.RFI_COMPLETED and not _is_rfi(rfq):
        raise HTTPException(
            status_code=400,
            detail="Only RFI documents can be completed with RFI_COMPLETED.",
        )
    if _is_rfi(rfq) and target_phase in RFI_BLOCKED_FORWARD_PHASES:
        raise HTTPException(
            status_code=400,
            detail=(
                "RFI documents cannot advance beyond Costing. "
                "Validate the pricing file to close the RFI."
            ),
        )
    if (
        _is_potential(rfq)
        and (target_phase, target_sub_status) != (rfq.phase, rfq.sub_status)
        and target_sub_status not in TERMINAL_SUBSTATUSES
    ):
        raise HTTPException(
            status_code=400,
            detail="Potential requests must be converted to RFQ before workflow advancement.",
        )


def _latest_pricing_file_link(rfq: Rfq) -> str:
    for entry in reversed(list(rfq.costing_files or [])):
        if not isinstance(entry, dict):
            continue
        file_role = str(entry.get("file_role") or "").strip().upper()
        if file_role != "PRICING_FINAL_PRICE":
            continue
        return str(
            entry.get("download_url")
            or entry.get("url")
            or entry.get("path")
            or entry.get("blob_url")
            or ""
        ).strip()
    return ""


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
    if rfq.phase != phase or rfq.sub_status != sub_status:
        rfq.last_notification_sent_at = None
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


async def _can_view_rfq(db: AsyncSession, current_user: User, rfq: Rfq) -> bool:
    if current_user.role == UserRole.OWNER:
        return True
    if current_user.role == UserRole.COSTING_TEAM:
        return await _is_assigned_costing_agent(db, current_user, rfq)
    if current_user.role == UserRole.RND:
        return rfq.phase == RfqPhase.COSTING and await _is_assigned_rnd(db, current_user, rfq)
    if current_user.role == UserRole.PLM:
        return await _is_assigned_plm(db, current_user, rfq)
    return (
        rfq.created_by_email == current_user.email
        or rfq.zone_manager_email == current_user.email
    )


async def _assert_can_view_rfq(db: AsyncSession, current_user: User, rfq: Rfq) -> None:
    if not await _can_view_rfq(db, current_user, rfq):
        raise HTTPException(status_code=403, detail="Not authorized to access this RFQ.")


def _is_rfq_creator(current_user: User, rfq: Rfq) -> bool:
    return _normalize_email(current_user.email) == _normalize_email(rfq.created_by_email)


def _is_assigned_validator(current_user: User, rfq: Rfq) -> bool:
    return _normalize_email(current_user.email) == _normalize_email(rfq.zone_manager_email)


def _is_costing_specialist_role(current_user: User) -> bool:
    return current_user.role in {UserRole.COSTING_TEAM, UserRole.RND, UserRole.PLM}


def _can_edit_rfq_phase(current_user: User, rfq: Rfq) -> bool:
    if current_user.role == UserRole.OWNER:
        return True
    if _is_costing_specialist_role(current_user):
        return False
    # In REVISION_REQUESTED, only the creator may make changes — the validator must wait.
    if rfq.sub_status == RfqSubStatus.REVISION_REQUESTED:
        return _is_rfq_creator(current_user, rfq)
    return _is_rfq_creator(current_user, rfq) or _is_assigned_validator(current_user, rfq)


def _can_edit_offer_phase(current_user: User, rfq: Rfq) -> bool:
    if current_user.role == UserRole.OWNER:
        return True
    if _is_costing_specialist_role(current_user):
        return False
    return _is_rfq_creator(current_user, rfq) or _is_assigned_validator(current_user, rfq)


def _assert_can_edit_rfq_phase(current_user: User, rfq: Rfq) -> None:
    if not _can_edit_rfq_phase(current_user, rfq):
        raise HTTPException(status_code=403, detail="Not authorized to edit this RFQ phase.")


def _assert_can_edit_offer_phase(current_user: User, rfq: Rfq) -> None:
    if not _can_edit_offer_phase(current_user, rfq):
        raise HTTPException(status_code=403, detail="Not authorized to edit this Offer phase.")


def _assert_can_edit_base_rfq_data(current_user: User, rfq: Rfq) -> None:
    if rfq.phase == RfqPhase.RFQ:
        _assert_can_edit_rfq_phase(current_user, rfq)
        return
    if rfq.phase == RfqPhase.OFFER:
        _assert_can_edit_offer_phase(current_user, rfq)
        return
    if current_user.role == UserRole.OWNER:
        return
    raise HTTPException(
        status_code=403,
        detail="Base RFQ data can only be changed from the RFQ or Offer phase.",
    )


async def _assert_can_directly_update_status(
    db: AsyncSession,
    current_user: User,
    rfq: Rfq,
    target_phase: RfqPhase,
) -> None:
    if rfq.phase == RfqPhase.COSTING:
        await _assert_costing_phase_assignment(
            db,
            current_user,
            rfq,
            allow_rnd=True,
            allow_plm=True,
        )
        return

    if rfq.phase == RfqPhase.RFQ:
        if current_user.role == UserRole.OWNER or _is_assigned_validator(
            current_user,
            rfq,
        ):
            return
        raise HTTPException(
            status_code=403,
            detail="Only the owner or assigned validator can directly update RFQ status.",
        )

    if rfq.phase == RfqPhase.OFFER:
        _assert_can_edit_offer_phase(current_user, rfq)
        return

    if target_phase == RfqPhase.COSTING:
        await _assert_costing_phase_assignment(
            db,
            current_user,
            rfq,
            allow_rnd=True,
            allow_plm=True,
        )
        return

    if target_phase == RfqPhase.RFQ:
        _assert_can_edit_rfq_phase(current_user, rfq)
        return

    if target_phase == RfqPhase.OFFER:
        _assert_can_edit_offer_phase(current_user, rfq)
        return

    if current_user.role == UserRole.OWNER:
        return

    raise HTTPException(status_code=403, detail="Not authorized to update this RFQ status.")


def _rfq_query():
    return select(Rfq).options(
        selectinload(Rfq.potential),
        selectinload(Rfq.offer_preparation),
    )


async def _refresh_rfq_response_state(db: AsyncSession, rfq: Rfq) -> None:
    # updated_at is populated by the database on UPDATE, so after a commit it can
    # remain in a server-postfetch state that is unsafe for FastAPI serialization
    # outside the async session context. Refresh the response-critical timestamps
    # explicitly before returning any RFQ payload.
    await db.refresh(
        rfq,
        attribute_names=["updated_at", "last_notification_sent_at"],
    )


async def _get_rfq_or_404(db: AsyncSession, rfq_id: str) -> Rfq:
    result = await db.execute(
        _rfq_query()
        .where(Rfq.rfq_id == rfq_id)
        .execution_options(populate_existing=True)
    )
    rfq = result.scalar_one_or_none()
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found.")
    await _refresh_rfq_response_state(db, rfq)
    rfq.rfq_data = normalize_rfq_data_products(rfq.rfq_data)
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


async def _get_offer_creator_profile(db: AsyncSession, rfq: Rfq) -> dict[str, str]:
    creator_email = str(rfq.created_by_email or "").strip()
    if not creator_email:
        return {}

    creator_user_result = await db.execute(select(User).where(User.email == creator_email))
    creator_user = creator_user_result.scalar_one_or_none()

    creator_contact_result = await db.execute(
        select(Contact).where(Contact.contact_email == creator_email)
    )
    creator_contact = creator_contact_result.scalar_one_or_none()

    creator_name = (
        str(creator_user.full_name or "").strip()
        if creator_user is not None
        else ""
    )
    if not creator_name and creator_contact is not None:
        creator_name = str(creator_contact.contact_name or "").strip()

    creator_phone = (
        str(creator_contact.contact_phone or "").strip()
        if creator_contact is not None
        else ""
    )

    return {
        "created_by_name": creator_name or creator_email,
        "created_by_phone": creator_phone,
        "created_by_email": creator_email,
    }


def _default_pricing_workflow_state() -> dict[str, object | None]:
    return {
        "workflow_state": PRICING_WORKFLOW_STATE_WAITING_BOM,
        "bom_file": None,
        "pricing_file": None,
        "validation_by": None,
        "validation_at": None,
        "rejection_reason": None,
    }


def _normalize_pricing_workflow_state(value: str | None) -> str:
    normalized_value = str(value or "").strip().upper()
    if normalized_value in PRICING_WORKFLOW_STATES:
        return normalized_value
    return ""


def _legacy_pricing_upload_from_rfq_data(rfq: Rfq, key: str) -> dict | None:
    rfq_data = dict(rfq.rfq_data or {})
    value = rfq_data.get(key)
    return value if isinstance(value, dict) else None


def _effective_pricing_workflow_state(rfq: Rfq) -> dict[str, object | None]:
    state = dict(rfq.costing_file_state or {})
    defaults = _default_pricing_workflow_state()
    bom_file = state.get("bom_file")
    pricing_file = state.get("pricing_file")

    if not isinstance(bom_file, dict):
        bom_file = _find_latest_costing_file_by_role(rfq, "PRICING_BOM")
    if not isinstance(bom_file, dict):
        bom_file = _legacy_pricing_upload_from_rfq_data(rfq, "pricing_bom_upload")

    if not isinstance(pricing_file, dict):
        pricing_file = _find_latest_costing_file_by_role(rfq, "PRICING_FINAL_PRICE")
    if not isinstance(pricing_file, dict):
        pricing_file = _legacy_pricing_upload_from_rfq_data(rfq, "pricing_final_price_upload")

    workflow_state = _normalize_pricing_workflow_state(state.get("workflow_state"))
    if not workflow_state:
        if state.get("rejection_reason") and pricing_file:
            workflow_state = PRICING_WORKFLOW_STATE_REJECTED
        elif (
            rfq.phase == RfqPhase.OFFER
            and rfq.sub_status == RfqSubStatus.PREPARATION
            and pricing_file
        ):
            workflow_state = PRICING_WORKFLOW_STATE_APPROVED
        elif pricing_file:
            workflow_state = PRICING_WORKFLOW_STATE_PRICING_UPLOADED
        elif bom_file:
            workflow_state = PRICING_WORKFLOW_STATE_BOM_UPLOADED
        elif rfq.phase == RfqPhase.COSTING and rfq.sub_status == RfqSubStatus.PRICING:
            workflow_state = PRICING_WORKFLOW_STATE_WAITING_BOM

    return {
        **defaults,
        **{key: state.get(key) for key in defaults.keys() if key != "workflow_state"},
        "workflow_state": workflow_state or None,
        "bom_file": bom_file,
        "pricing_file": pricing_file,
    }


def _set_pricing_workflow_state(rfq: Rfq, **updates: object | None) -> None:
    next_state = dict(rfq.costing_file_state or {})
    effective_state = _effective_pricing_workflow_state(rfq)
    defaults = _default_pricing_workflow_state()

    for key in defaults.keys():
        next_state[key] = effective_state.get(key)

    next_state.update(updates)
    rfq.costing_file_state = next_state


async def _is_assigned_plm(db: AsyncSession, current_user: User, rfq: Rfq) -> bool:
    email = await resolve_product_line_role_email(
        db,
        role=ProductLineRoutingRole.PLM,
        acronym=rfq.product_line_acronym,
    )
    return _normalize_email(current_user.email) == _normalize_email(email)


async def _is_assigned_costing_agent(db: AsyncSession, current_user: User, rfq: Rfq) -> bool:
    email = await resolve_product_line_role_email(
        db,
        role=ProductLineRoutingRole.COSTING,
        acronym=rfq.product_line_acronym,
    )
    return _normalize_email(current_user.email) == _normalize_email(email)


async def _is_assigned_rnd(db: AsyncSession, current_user: User, rfq: Rfq) -> bool:
    email = await resolve_product_line_role_email(
        db,
        role=ProductLineRoutingRole.RND,
        acronym=rfq.product_line_acronym,
    )
    return _normalize_email(current_user.email) == _normalize_email(email)


async def _assert_costing_phase_assignment(
    db: AsyncSession,
    current_user: User,
    rfq: Rfq,
    *,
    allow_rnd: bool = False,
    allow_plm: bool = False,
) -> None:
    if current_user.role == UserRole.OWNER:
        return

    if current_user.role == UserRole.COSTING_TEAM:
        if not await _is_assigned_costing_agent(db, current_user, rfq):
            raise HTTPException(
                status_code=403,
                detail="You are not assigned as the costing agent for this RFQ.",
            )
        return

    if allow_rnd and current_user.role == UserRole.RND:
        if not await _is_assigned_rnd(db, current_user, rfq):
            raise HTTPException(
                status_code=403,
                detail="You are not assigned as the R&D contact for this RFQ.",
            )
        return

    if allow_plm and current_user.role == UserRole.PLM:
        if not await _is_assigned_plm(db, current_user, rfq):
            raise HTTPException(
                status_code=403,
                detail="You are not assigned as the PLM for this RFQ.",
            )
        return

    raise HTTPException(
        status_code=403,
        detail="You are not authorized to perform costing actions for this RFQ.",
    )


def _append_revision_note(existing_notes: str | None, prefix: str, detail: str | None) -> str:
    note_line = f"{prefix}{str(detail or '').strip()}".strip()
    current_notes = str(existing_notes or "").strip()
    if not note_line:
        return current_notes
    if not current_notes:
        return note_line
    return f"{current_notes}\n{note_line}"


def _default_costing_file_state() -> dict[str, str | None]:
    return {
        "file_status": COSTING_FILE_STATUS_PENDING,
        "file_note": None,
        "action_by": None,
        "action_at": None,
        "file": None,
    }


def _build_costing_file_entry(
    file_meta: dict[str, str] | None,
    *,
    file_role: str,
    phase: RfqSubStatus,
    note: str | None = None,
) -> dict[str, str]:
    entry = dict(file_meta or {})
    entry["file_role"] = file_role
    entry["phase"] = phase.value
    if note is not None:
        entry["note"] = note
    return entry


def _find_latest_costing_file_by_role(rfq: Rfq, file_role: str) -> dict | None:
    target_role = str(file_role or "").strip().upper()
    if not target_role:
        return None

    entries = [
        entry
        for entry in list(rfq.costing_files or [])
        if str(entry.get("file_role") or "").strip().upper() == target_role
    ]
    return entries[-1] if entries else None


def _effective_costing_file_state(rfq: Rfq) -> dict:
    state = dict(rfq.costing_file_state or {})
    status = str(state.get("file_status") or "").strip().upper()
    if status in {
        COSTING_FILE_STATUS_PENDING,
        COSTING_FILE_STATUS_UPLOADED,
        COSTING_FILE_STATUS_NA,
    }:
        return state

    legacy_files = [
        entry
        for entry in list(rfq.costing_files or [])
        if str(entry.get("file_role") or "").strip().upper()
        not in PRICING_COSTING_FILE_ROLES
    ]
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


_SELF_REVISION_COMMENT = "self-update initiated by assigned validator."


def _build_revision_chat_greeting(revision_notes: str) -> str:
    notes = str(revision_notes or "").strip()
    if not notes or notes.casefold() == _SELF_REVISION_COMMENT:
        return "Please tell me your updates."
    return (
        f"The validator requested the following updates: {notes}. "
        "What would you like to change?"
    )


def _ensure_revision_greeting_in_history(rfq: Rfq) -> bool:
    """Append the revision greeting to chat_history if not already present.
    Returns True if the history was modified."""
    if not (
        rfq.phase == RfqPhase.RFQ
        and rfq.sub_status == RfqSubStatus.REVISION_REQUESTED
    ):
        return False
    greeting = _build_revision_chat_greeting(rfq.revision_notes)
    history = list(rfq.chat_history or [])
    already_present = any(
        m.get("role") == "assistant" and m.get("content") == greeting
        for m in history
    )
    if already_present:
        return False
    history.append({"role": "assistant", "content": greeting})
    rfq.chat_history = history
    return True

async def _upload_costing_action_file(
    *,
    rfq_id: str,
    file: UploadFile,
    current_user_email: str,
    folder_name: str = "costing",
) -> dict[str, str]:
    safe_name = _safe_upload_filename(file.filename)
    file_id = str(uuid.uuid4())
    blob_name = f"{rfq_id}/{folder_name}/{file_id}-{safe_name}"

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
    if _is_potential(rfq):
        allowed = {
            (phase, sub_status)
            for phase, sub_status in allowed
            if sub_status in TERMINAL_SUBSTATUSES
        }
    elif _is_rfi(rfq):
        allowed = {
            (phase, sub_status)
            for phase, sub_status in allowed
            if phase not in RFI_BLOCKED_FORWARD_PHASES
        }
    else:
        allowed = {
            (phase, sub_status)
            for phase, sub_status in allowed
            if sub_status != RfqSubStatus.RFI_COMPLETED
        }

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


async def _resolve_validation_matrix_product(
    db: AsyncSession,
    product_name: str | None,
) -> ValidationMatrix | None:
    normalized_name = str(product_name or "").strip()
    if not normalized_name:
        return None

    result = await db.execute(
        select(ValidationMatrix).where(
            or_(
                func.lower(ValidationMatrix.product_line) == normalized_name.casefold(),
                func.lower(ValidationMatrix.acronym) == normalized_name.casefold(),
            )
        )
    )
    return result.scalar_one_or_none()


async def _sync_product_line_from_product_name(
    db: AsyncSession,
    rfq: Rfq,
    rfq_data: dict,
    *,
    force: bool = False,
) -> dict:
    next_data = dict(rfq_data or {})
    product_name = str(next_data.get("product_name") or "").strip()
    if not product_name:
        return next_data

    matrix = await _resolve_validation_matrix_product(db, product_name)
    if matrix is None:
        if force:
            next_data.pop("product_line_acronym", None)
            rfq.product_line_acronym = None
        return next_data

    next_data["product_name"] = matrix.product_line
    next_data["product_line_acronym"] = matrix.acronym
    rfq.product_line_acronym = matrix.acronym
    return next_data


def _raise_for_conflicting_product_currencies(rfq_data: dict | None) -> None:
    conflicting_currencies = get_conflicting_product_currencies(rfq_data)
    if not conflicting_currencies:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "All product rows in one RFQ/RFI must use the same currency. "
            f"Found: {', '.join(conflicting_currencies)}."
        ),
    )


async def _sync_rfq_product_derived_fields(
    rfq_data: dict | None,
    *,
    db3: AsyncSession | None,
    require_strict_fx: bool = False,
) -> dict:
    next_data = normalize_rfq_data_products(rfq_data)
    products = next_data.get("products")
    if not isinstance(products, list) or not products:
        next_data.pop("to_total_local", None)
        return next_data

    total_target_to = sum(
        float(product.get("target_to") or 0.0)
        for product in products
        if isinstance(product, dict)
    )
    next_data["total_target_to"] = total_target_to

    first_product = products[0] if products else {}
    first_local_target_price = (
        first_product.get("target_price")
        if isinstance(first_product.get("target_price"), (int, float))
        else None
    )
    existing_target_price_eur = next_data.get("target_price_eur")
    shared_currency = str(
        (first_product or {}).get("currency")
        or next_data.get("target_price_currency")
        or "EUR"
    ).strip().upper() or "EUR"
    next_data["target_price_currency"] = shared_currency
    next_data["target_price_local"] = (
        first_local_target_price if first_local_target_price is not None else ""
    )

    routing_total_target_to = total_target_to
    if shared_currency != "EUR":
        if db3 is None:
            if require_strict_fx:
                raise ValueError(
                    f"FX lookup is unavailable for {shared_currency}. "
                    "Please restate the target prices directly in EUR."
                )
            next_data["to_total_local"] = total_target_to / 1000.0
            next_data["to_total"] = total_target_to / 1000.0
            next_data["target_price_eur"] = (
                existing_target_price_eur
                if existing_target_price_eur not in (None, "")
                else ""
            )
            return next_data

        eur_rate = await get_eur_exchange_rate(shared_currency, db3=db3)
        fallback_used = bool(shared_currency and eur_rate == 1.0)
        if fallback_used and require_strict_fx:
            raise ValueError(
                f"FX lookup fallback prevented validator routing for {shared_currency}. "
                "Please restate the target prices directly in EUR."
            )

        routing_total_target_to = sum(
            float(product.get("target_to") or 0.0) * eur_rate
            for product in products
            if isinstance(product, dict)
        )
        next_data["to_total_local"] = total_target_to / 1000.0
        next_data["target_price_eur"] = (
            first_local_target_price * eur_rate
            if first_local_target_price is not None and not fallback_used
            else existing_target_price_eur
            if existing_target_price_eur not in (None, "")
            else ""
        )
    else:
        next_data.pop("to_total_local", None)
        next_data["target_price_eur"] = (
            first_local_target_price if first_local_target_price is not None else ""
        )

    next_data["to_total"] = routing_total_target_to / 1000.0
    return next_data


async def _maybe_assign_systematic_rfq_id(
    db: AsyncSession,
    rfq: Rfq,
    rfq_data: dict,
) -> dict:
    next_data = normalize_rfq_data_products(rfq_data)
    if _is_potential(rfq):
        return next_data
    next_data = await _sync_product_line_from_product_name(db, rfq, next_data)
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
    _assert_can_edit_rfq_phase(current_user, rfq)

    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Only RFQs in RFQ/NEW_RFQ can be submitted for validation. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )
    if _is_potential(rfq):
        raise HTTPException(
            status_code=409,
            detail="Convert this Potential request to RFQ before submitting it for validation.",
        )

    extracted_data = normalize_rfq_data_products(rfq.rfq_data)
    extracted_data = await _sync_product_line_from_product_name(db, rfq, extracted_data)
    _raise_for_conflicting_product_currencies(extracted_data)
    incomplete_product_fields = get_incomplete_product_fields(extracted_data)
    if incomplete_product_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Complete all product rows before validation.",
                "missing_fields": incomplete_product_fields,
            },
        )

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
        if email_sent:
            rfq.last_notification_sent_at = datetime.datetime.utcnow()
            await record_notification_sent(
                db,
                rfq_id=rfq.rfq_id,
                recipients=zone_manager_email,
                email_type=EMAIL_VALIDATION_REQUEST,
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
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(
        require_role(UserRole.COMMERCIAL, UserRole.OWNER, UserRole.ZONE_MANAGER)
    ),
):
    request_body = body or RfqCreateRequest()
    chat_mode = request_body.chat_mode.lower().strip()
    document_type = request_body.document_type
    if chat_mode == "potential" or document_type == RfqDocumentType.POTENTIAL:
        chat_mode = "potential"
        document_type = RfqDocumentType.POTENTIAL
    initial_sub_status = RfqSubStatus.NEW_RFQ
    rfq_data = rfq_data_payload_to_dict(request_body.rfq_data)
    zone_manager_email = (
        rfq_data.get("zone_manager_email") or rfq_data.get("validator_email") or None
    )

    rfq = Rfq(
        document_type=document_type,
        phase=RfqPhase.RFQ,
        sub_status=initial_sub_status,
        product_line_acronym=rfq_data.get("product_line_acronym"),
        zone_manager_email=zone_manager_email,
        created_by_email=current_user.email,
        rfq_data=rfq_data,
        chat_history=[],
    )
    rfq_data = await _sync_product_line_from_product_name(
        db,
        rfq,
        rfq_data,
        force="product_name" in rfq_data,
    )
    _raise_for_conflicting_product_currencies(rfq_data)
    rfq_data = await _sync_rfq_product_derived_fields(rfq_data, db3=db3)
    zone_manager_email = (
        rfq_data.get("zone_manager_email") or rfq_data.get("validator_email") or None
    )
    rfq.product_line_acronym = rfq_data.get("product_line_acronym")
    rfq.zone_manager_email = zone_manager_email
    rfq.rfq_data = rfq_data
    rfq.rfq_data = await _maybe_assign_systematic_rfq_id(db, rfq, rfq_data)
    db.add(rfq)
    if document_type == RfqDocumentType.POTENTIAL:
        rfq.potential = Potential(chat_history=[])

    await db.commit()
    return await _get_rfq_or_404(db, rfq.rfq_id)


@router.get("/fx/eur-rate", response_model=RfqFxRateOut)
async def get_rfq_eur_fx_rate(
    currency_code: str = Query(..., min_length=1),
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(get_current_user),
):
    del current_user
    sanitized_currency = "".join(
        char for char in str(currency_code or "").upper() if char.isalpha()
    )
    eur_rate = await get_eur_exchange_rate(sanitized_currency, db3=db3)
    fallback_used = bool(
        sanitized_currency and sanitized_currency != "EUR" and eur_rate == 1.0
    )
    return RfqFxRateOut(
        currency_code=sanitized_currency,
        eur_rate=eur_rate,
        fallback_used=fallback_used,
    )


@router.put("/{rfq_id}/data", response_model=RfqOut)
async def update_rfq_data(
    rfq_id: str,
    body: RfqDataUpdateRequest,
    db: AsyncSession = Depends(get_db),
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_edit_base_rfq_data(current_user, rfq)

    incoming_data = rfq_data_payload_to_dict(body.rfq_data)
    incoming_data.pop("rfq_files", None)
    next_data = dict(rfq.rfq_data or {})
    next_data.update(incoming_data)
    next_data = normalize_rfq_data_products(
        next_data,
        products_authoritative="products" in incoming_data,
    )
    next_data = await _sync_product_line_from_product_name(
        db,
        rfq,
        next_data,
        force="product_name" in incoming_data,
    )
    _raise_for_conflicting_product_currencies(next_data)
    next_data = await _sync_rfq_product_derived_fields(next_data, db3=db3)
    rfq.rfq_data = next_data

    if "product_line_acronym" in incoming_data or "product_name" in incoming_data:
        rfq.product_line_acronym = next_data.get("product_line_acronym")
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
    body: ProceedToFormalRequest | None = None,
    db: AsyncSession = Depends(get_db),
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(get_current_user),
):
    request_body = body or ProceedToFormalRequest()
    target_type = request_body.document_type
    if target_type not in {RfqDocumentType.RFQ, RfqDocumentType.RFI}:
        raise HTTPException(
            status_code=400,
            detail="Potential can only be converted to RFQ or RFI.",
        )

    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_edit_rfq_phase(current_user, rfq)

    if not _is_potential(rfq):
        raise HTTPException(
            status_code=409,
            detail="This opportunity is no longer a Potential request.",
        )
    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ):
        raise HTTPException(
            status_code=409,
            detail="Only Potential requests in RFQ/NEW_RFQ can be converted to RFQ or RFI.",
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

    rfq.rfq_data = normalize_rfq_data_products(
        sync_potential_to_rfq_data(potential, rfq.rfq_data)
    )
    rfq.rfq_data = await _sync_rfq_product_derived_fields(rfq.rfq_data, db3=db3)
    rfq.document_type = target_type
    rfq.phase = RfqPhase.RFQ
    rfq.sub_status = RfqSubStatus.NEW_RFQ
    rfq.last_notification_sent_at = None

    label = target_type.value
    await log_action(db, rfq_id, f"Potential promoted to formal {label}", current_user.email)
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
    _assert_can_edit_base_rfq_data(current_user, rfq)

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
    raw_files = extracted_data.get("rfq_files")
    if isinstance(raw_files, list):
        existing_files = list(raw_files)
    else:
        # If it's a boolean placeholder or missing, start a fresh metadata list.
        existing_files = []
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
    _assert_can_edit_base_rfq_data(current_user, rfq)

    extracted_data = dict(rfq.rfq_data or {})
    raw_files = extracted_data.get("rfq_files")
    existing_files = list(raw_files) if isinstance(raw_files, list) else []

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
    _assert_can_edit_base_rfq_data(current_user, rfq)

    extracted_data = dict(rfq.rfq_data or {})
    raw_files = extracted_data.get("rfq_files")
    existing_files = list(raw_files) if isinstance(raw_files, list) else []

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
    document_type: list[str] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = _rfq_query().order_by(Rfq.updated_at.desc(), Rfq.created_at.desc())

    document_type_filters = _parse_document_type_filters(document_type)
    if document_type_filters:
        query = query.where(Rfq.document_type.in_(document_type_filters))

    if current_user.role != UserRole.OWNER:
        if current_user.role in {UserRole.COSTING_TEAM, UserRole.RND, UserRole.PLM}:
            routing_role = {
                UserRole.COSTING_TEAM: ProductLineRoutingRole.COSTING,
                UserRole.RND: ProductLineRoutingRole.RND,
                UserRole.PLM: ProductLineRoutingRole.PLM,
            }[current_user.role]
            assigned_acronyms = await get_assigned_product_line_acronyms(
                db,
                role=routing_role,
                email=current_user.email,
            )
            if not assigned_acronyms:
                query = query.where(Rfq.rfq_id == "__unassigned__")
            else:
                query = query.where(Rfq.product_line_acronym.in_(assigned_acronyms))
                if current_user.role == UserRole.RND:
                    query = query.where(Rfq.phase == RfqPhase.COSTING)
        else:
            visibility_filters = [
                Rfq.created_by_email == current_user.email,
                Rfq.zone_manager_email == current_user.email,
            ]
            query = query.where(or_(*visibility_filters))

    result = await db.execute(query)
    rfqs = result.scalars().all()
    for rfq in rfqs:
        rfq.rfq_data = normalize_rfq_data_products(rfq.rfq_data)
    return rfqs


@router.get("/{rfq_id}", response_model=RfqOut)
async def get_rfq(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_view_rfq(db, current_user, rfq)
    if rfq.chat_history and _ensure_revision_greeting_in_history(rfq):
        await db.commit()
        await db.refresh(rfq)
    return rfq


@router.get("/{rfq_id}/discussion", response_model=list[DiscussionMessageOut])
async def get_rfq_discussion(
    rfq_id: str,
    phase: RfqSubStatus,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_view_rfq(db, current_user, rfq)

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
    await _assert_can_view_rfq(db, current_user, rfq)

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
    await _assert_can_view_rfq(db, current_user, rfq)

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
    await _assert_can_view_rfq(db, current_user, rfq)
    await _assert_costing_phase_assignment(
        db,
        current_user,
        rfq,
        allow_rnd=True,
        allow_plm=True,
    )

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
    email_sent = emails.send_costing_message_email(
        body.recipient_email,
        str(rfq_data.get("systematic_rfq_id") or ""),
        current_user.email,
        body.message,
        _build_rfq_link(rfq_id),
    )
    if email_sent:
        await record_notification_sent(
            db,
            rfq_id=rfq_id,
            recipients=body.recipient_email,
            email_type=EMAIL_COSTING_MESSAGE,
        )

    return _build_discussion_message_out(message, current_user)


@router.get("/{rfq_id}/costing-template")
async def download_costing_template(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_view_rfq(db, current_user, rfq)

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


@router.get("/{rfq_id}/offer-template/preview")
async def preview_offer_template(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_view_rfq(db, current_user, rfq)

    if rfq.phase != RfqPhase.OFFER:
        raise HTTPException(
            status_code=400,
            detail="The offer preparation template preview is only available during the Offer phase.",
        )

    try:
        creator_profile = await _get_offer_creator_profile(db, rfq)
        preview_html = render_offer_preparation_preview_html(
            rfq,
            creator_profile=creator_profile,
            offer_data=get_offer_preparation_data_snapshot(rfq),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to generate the offer preparation preview. {exc}",
        ) from exc

    return {
        "html": preview_html,
        "filename": build_offer_preparation_filename(rfq),
    }


@router.get("/{rfq_id}/offer-template")
async def download_offer_template(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_view_rfq(db, current_user, rfq)

    if rfq.phase != RfqPhase.OFFER:
        raise HTTPException(
            status_code=400,
            detail="The offer preparation template is only available during the Offer phase.",
        )

    filename = build_offer_preparation_filename(rfq)
    try:
        creator_profile = await _get_offer_creator_profile(db, rfq)
        document_docx = render_offer_preparation_docx(
            rfq,
            creator_profile=creator_profile,
            offer_data=get_offer_preparation_data_snapshot(rfq),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to generate the offer preparation document. {exc}",
        ) from exc

    return Response(
        content=document_docx,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
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

    if current_user.role != UserRole.OWNER and not _is_assigned_validator(current_user, rfq):
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
    _ensure_revision_greeting_in_history(rfq)

    await log_action(
        db,
        rfq_id,
        f"Revision requested -> {RfqPhase.RFQ.value}/{RfqSubStatus.REVISION_REQUESTED.value}: {comment}",
        current_user.email,
    )
    await db.commit()
    await db.refresh(rfq)

    rfq_data = dict(rfq.rfq_data or {})
    email_sent = emails.send_revision_request_email(
        rfq.created_by_email,
        str(rfq_data.get("systematic_rfq_id") or ""),
        comment,
        _build_rfq_link(rfq_id),
    )
    if email_sent:
        await record_notification_sent(
            db,
            rfq_id=rfq_id,
            recipients=rfq.created_by_email,
            email_type=EMAIL_REVISION_REQUEST,
        )

    return await _get_rfq_or_404(db, rfq_id)


@router.post("/{rfq_id}/submit-revision", response_model=RfqOut)
async def submit_revision(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    _assert_can_edit_rfq_phase(current_user, rfq)

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
        require_role(
            UserRole.COMMERCIAL,
            UserRole.ZONE_MANAGER,
            UserRole.COSTING_TEAM,
            UserRole.RND,
            UserRole.PLANT_MANAGER,
            UserRole.PLM,
            UserRole.OWNER,
        )
    ),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_directly_update_status(db, current_user, rfq, body.phase)

    effective_phase = body.phase
    if body.sub_status in TERMINAL_SUBSTATUSES:
        effective_phase = rfq.phase
        _assert_terminal_status_allowed(rfq, body.sub_status)
    _assert_document_type_allows_target(rfq, effective_phase, body.sub_status)
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
    await _assert_can_view_rfq(db, current_user, rfq)

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
    await _assert_can_view_rfq(db, current_user, rfq)

    query = select(AuditLog).where(AuditLog.rfq_id == rfq_id).order_by(AuditLog.timestamp.desc())
    logs = await db.execute(query)
    return logs.scalars().all()


@router.get("/{rfq_id}/notifications", response_model=list[NotificationLogOut])
async def get_notification_logs(
    rfq_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_can_view_rfq(db, current_user, rfq)

    query = (
        select(NotificationLog)
        .where(NotificationLog.rfq_id == rfq_id)
        .order_by(NotificationLog.sent_at.desc(), NotificationLog.log_id.desc())
    )
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

    if current_user.role != UserRole.OWNER and not _is_assigned_validator(current_user, rfq):
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
        route_entry = await resolve_product_line_role_assignment(
            db,
            role=ProductLineRoutingRole.COSTING,
            acronym=refreshed_rfq.product_line_acronym,
        )
        if route_entry:
            refreshed_data = dict(refreshed_rfq.rfq_data or {})
            recipient_email = str(route_entry.get("email") or "")
            email_sent = emails.send_costing_entry_email(
                recipient_email,
                str(route_entry.get("product_line") or ""),
                str(route_entry.get("acronym") or ""),
                str(refreshed_data.get("systematic_rfq_id") or ""),
                _build_rfq_link(refreshed_rfq.rfq_id),
            )
            if email_sent:
                refreshed_rfq.last_notification_sent_at = datetime.datetime.utcnow()
                await record_notification_sent(
                    db,
                    rfq_id=refreshed_rfq.rfq_id,
                    recipients=recipient_email,
                    email_type=EMAIL_COSTING_ENTRY,
                )

    await _refresh_rfq_response_state(db, refreshed_rfq)
    return refreshed_rfq


@router.post("/{rfq_id}/costing_review", response_model=RfqOut)
async def costing_review(
    rfq_id: str,
    body: CostingReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_costing_phase_assignment(db, current_user, rfq)

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
    reception_email_sent = emails.send_costing_reception_results_email(
        refreshed_rfq.created_by_email,
        cc_email,
        current_user.email,
        systematic_rfq_id,
        rfq_link,
        is_approved=body.scope,
        rejection_reason=body.rejection_reason,
    )
    if reception_email_sent:
        await record_notification_sent(
            db,
            rfq_id=refreshed_rfq.rfq_id,
            recipients=[refreshed_rfq.created_by_email, cc_email or ""],
            email_type=EMAIL_COSTING_RECEPTION_RESULT,
        )

    if body.scope:
        route_entry = await resolve_product_line_role_assignment(
            db,
            role=ProductLineRoutingRole.COSTING,
            acronym=refreshed_rfq.product_line_acronym,
        )
        if route_entry:
            recipient_email = str(route_entry.get("email") or "")
            handoff_email_sent = emails.send_costing_handoff_email(
                recipient_email,
                str(route_entry.get("product_line") or ""),
                str(route_entry.get("acronym") or ""),
                systematic_rfq_id,
                rfq_link,
            )
            if handoff_email_sent:
                refreshed_rfq.last_notification_sent_at = datetime.datetime.utcnow()
                await record_notification_sent(
                    db,
                    rfq_id=refreshed_rfq.rfq_id,
                    recipients=recipient_email,
                    email_type=EMAIL_COSTING_HANDOFF,
                )
        if str(refreshed_rfq.product_line_acronym or "").upper() == "ASS":
            await sync_rfq_to_assembly(refreshed_rfq)

    await _refresh_rfq_response_state(db, refreshed_rfq)
    return refreshed_rfq


@router.post("/{rfq_id}/costing-file-action", response_model=RfqOut)
async def submit_costing_file_action(
    rfq_id: str,
    action: str = Form(...),
    note: str = Form(...),
    feasibility_status: str = Form(...),
    file: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.RND, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_costing_phase_assignment(db, current_user, rfq, allow_rnd=True)

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

    normalized_feasibility_status = str(feasibility_status or "").strip().upper()
    if normalized_feasibility_status not in FEASIBILITY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "feasibility_status must be one of "
                "'FEASIBLE', 'FEASIBLE_UNDER_CONDITION', or 'NOT_FEASIBLE'."
            ),
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
        rfq.costing_files = list(rfq.costing_files or []) + [
            _build_costing_file_entry(
                file_meta,
                file_role="FEASIBILITY",
                phase=rfq.sub_status,
                note=trimmed_note,
            )
        ]

    next_costing_file_state = dict(rfq.costing_file_state or {})
    next_costing_file_state.update(
        {
            "file_status": normalized_action,
            "file_note": trimmed_note,
            "feasibility_status": normalized_feasibility_status,
            "action_by": current_user.email,
            "action_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "file": file_meta,
        }
    )
    rfq.costing_file_state = next_costing_file_state

    action_label = (
        "Not applicable noted"
        if normalized_action == COSTING_FILE_STATUS_NA
        else "Costing file uploaded"
    )
    await log_action(
        db,
        rfq_id,
        f"{action_label} [{normalized_feasibility_status}]: {trimmed_note}",
        current_user.email,
    )
    await db.commit()
    refreshed_rfq = await _get_rfq_or_404(db, rfq_id)

    if current_user.role == UserRole.RND:
        refreshed_data = dict(refreshed_rfq.rfq_data or {})
        systematic_rfq_id = str(refreshed_data.get("systematic_rfq_id") or "")
        email_sent = emails.send_feasibility_result_email(
            recipient_email=refreshed_rfq.created_by_email,
            systematic_rfq_id=systematic_rfq_id,
            feasibility_status=normalized_feasibility_status,
            rfq_link=_build_rfq_link(rfq_id),
        )
        if email_sent:
            await record_notification_sent(
                db,
                rfq_id=rfq_id,
                recipients=refreshed_rfq.created_by_email,
                email_type=EMAIL_FEASIBILITY_RESULT,
            )

    await _refresh_rfq_response_state(db, refreshed_rfq)
    return refreshed_rfq


@router.post("/{rfq_id}/pricing-bom", response_model=RfqOut)
async def upload_pricing_bom_file(
    rfq_id: str,
    note: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_costing_phase_assignment(db, current_user, rfq)

    if rfq.phase != RfqPhase.COSTING or rfq.sub_status != RfqSubStatus.PRICING:
        raise HTTPException(
            status_code=400,
            detail=(
                "Pricing BOM uploads are only allowed during COSTING/PRICING. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    pricing_workflow_state = _effective_pricing_workflow_state(rfq)
    if pricing_workflow_state.get("workflow_state") != PRICING_WORKFLOW_STATE_WAITING_BOM:
        raise HTTPException(
            status_code=400,
            detail=(
                "BOM upload is only allowed when the pricing workflow is waiting for BOM data."
            ),
        )

    trimmed_note = str(note or "").strip()
    if not trimmed_note:
        raise HTTPException(status_code=400, detail="note is required.")

    file_meta = await _upload_costing_action_file(
        rfq_id=rfq_id,
        file=file,
        current_user_email=current_user.email,
        folder_name="pricing",
    )
    costing_file_entry = _build_costing_file_entry(
        file_meta,
        file_role="PRICING_BOM",
        phase=RfqSubStatus.PRICING,
        note=trimmed_note,
    )

    rfq_data = dict(rfq.rfq_data or {})
    rfq_data.pop("pricing_bom_upload", None)
    rfq.rfq_data = rfq_data
    rfq.costing_files = list(rfq.costing_files or []) + [costing_file_entry]
    _set_pricing_workflow_state(
        rfq,
        workflow_state=PRICING_WORKFLOW_STATE_BOM_UPLOADED,
        bom_file=costing_file_entry,
        pricing_file=None,
        validation_by=None,
        validation_at=None,
        rejection_reason=None,
    )

    await log_action(
        db,
        rfq_id,
        f"Pricing BOM file uploaded: {trimmed_note}",
        current_user.email,
    )
    await db.commit()
    refreshed_rfq = await _get_rfq_or_404(db, rfq_id)
    refreshed_data = dict(refreshed_rfq.rfq_data or {})
    recipient_email = await resolve_product_line_role_email(
        db,
        role=ProductLineRoutingRole.COSTING,
        acronym=refreshed_rfq.product_line_acronym,
    ) or ""
    email_sent = emails.send_bom_ready_email(
        recipient_email,
        str(refreshed_data.get("systematic_rfq_id") or ""),
        _build_rfq_link(refreshed_rfq.rfq_id),
    )
    if email_sent:
        await record_notification_sent(
            db,
            rfq_id=refreshed_rfq.rfq_id,
            recipients=recipient_email,
            email_type=EMAIL_BOM_READY,
        )
    await _refresh_rfq_response_state(db, refreshed_rfq)
    return refreshed_rfq


@router.post("/{rfq_id}/pricing-final-price", response_model=RfqOut)
async def upload_pricing_final_price_file(
    rfq_id: str,
    note: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.COSTING_TEAM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_costing_phase_assignment(db, current_user, rfq)

    if rfq.phase != RfqPhase.COSTING or rfq.sub_status != RfqSubStatus.PRICING:
        raise HTTPException(
            status_code=400,
            detail=(
                "Final price uploads are only allowed during COSTING/PRICING. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    pricing_workflow_state = _effective_pricing_workflow_state(rfq)
    workflow_state = pricing_workflow_state.get("workflow_state")
    if workflow_state not in {
        PRICING_WORKFLOW_STATE_BOM_UPLOADED,
        PRICING_WORKFLOW_STATE_REJECTED,
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "Final price uploads are only allowed after the BOM upload or after a rejection."
            ),
        )

    has_pricing_bom_file = isinstance(pricing_workflow_state.get("bom_file"), dict)
    if not has_pricing_bom_file:
        raise HTTPException(
            status_code=400,
            detail=(
                "Upload the costing file with BOM data before adding the final price file."
            ),
        )

    trimmed_note = str(note or "").strip()
    if not trimmed_note:
        raise HTTPException(status_code=400, detail="note is required.")

    file_meta = await _upload_costing_action_file(
        rfq_id=rfq_id,
        file=file,
        current_user_email=current_user.email,
        folder_name="pricing-final-price",
    )
    costing_file_entry = _build_costing_file_entry(
        file_meta,
        file_role="PRICING_FINAL_PRICE",
        phase=RfqSubStatus.PRICING,
        note=trimmed_note,
    )

    rfq_data = dict(rfq.rfq_data or {})
    rfq_data.pop("pricing_final_price_upload", None)
    rfq.rfq_data = rfq_data
    rfq.costing_files = list(rfq.costing_files or []) + [costing_file_entry]
    _set_pricing_workflow_state(
        rfq,
        workflow_state=PRICING_WORKFLOW_STATE_PRICING_UPLOADED,
        pricing_file=costing_file_entry,
        validation_by=None,
        validation_at=None,
        rejection_reason=None,
    )

    await log_action(
        db,
        rfq_id,
        f"Pricing final price file uploaded: {trimmed_note}",
        current_user.email,
    )
    await db.commit()
    refreshed_rfq = await _get_rfq_or_404(db, rfq_id)
    refreshed_data = dict(refreshed_rfq.rfq_data or {})
    recipient_email = await resolve_product_line_role_email(
        db,
        role=ProductLineRoutingRole.PLM,
        acronym=refreshed_rfq.product_line_acronym,
    ) or ""
    email_sent = emails.send_pricing_ready_email(
        recipient_email,
        str(refreshed_data.get("systematic_rfq_id") or ""),
        _build_rfq_link(refreshed_rfq.rfq_id),
    )
    if email_sent:
        await record_notification_sent(
            db,
            rfq_id=refreshed_rfq.rfq_id,
            recipients=recipient_email,
            email_type=EMAIL_PRICING_READY,
        )
    await _refresh_rfq_response_state(db, refreshed_rfq)
    return refreshed_rfq


@router.post("/{rfq_id}/costing_validation", response_model=RfqOut)
async def costing_validation(
    rfq_id: str,
    body: CostingValidationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.PLM, UserRole.OWNER)),
):
    rfq = await _get_rfq_or_404(db, rfq_id)
    await _assert_costing_phase_assignment(db, current_user, rfq, allow_plm=True)

    if (rfq.phase, rfq.sub_status) != (RfqPhase.COSTING, RfqSubStatus.PRICING):
        raise HTTPException(
            status_code=400,
            detail=(
                "Costing validation is only allowed during COSTING/PRICING. "
                f"Current state: {rfq.phase.value}/{rfq.sub_status.value}."
            ),
        )

    pricing_workflow_state = _effective_pricing_workflow_state(rfq)
    if pricing_workflow_state.get("workflow_state") != PRICING_WORKFLOW_STATE_PRICING_UPLOADED:
        raise HTTPException(
            status_code=400,
            detail="The pricing workflow is not ready for validation.",
        )

    validation_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    refreshed_data = dict(rfq.rfq_data or {})
    systematic_rfq_id = str(refreshed_data.get("systematic_rfq_id") or "")
    rfq_link = _build_rfq_link(rfq.rfq_id)

    if body.is_approved:
        _set_pricing_workflow_state(
            rfq,
            workflow_state=PRICING_WORKFLOW_STATE_APPROVED,
            validation_by=current_user.email,
            validation_at=validation_at,
            rejection_reason=None,
        )
        if _is_rfi(rfq):
            _set_phase_sub_status(rfq, RfqPhase.CLOSED, RfqSubStatus.RFI_COMPLETED)
            log_message = (
                f"RFI pricing file approved -> "
                f"{RfqPhase.CLOSED.value}/{RfqSubStatus.RFI_COMPLETED.value}"
            )
        else:
            _set_phase_sub_status(rfq, RfqPhase.OFFER, RfqSubStatus.PREPARATION)
            log_message = (
                f"Pricing file approved -> "
                f"{RfqPhase.OFFER.value}/{RfqSubStatus.PREPARATION.value}"
            )
        await log_action(
            db,
            rfq_id,
            log_message,
            current_user.email,
        )
    else:
        rejection_reason = str(body.rejection_reason or "").strip()
        _set_pricing_workflow_state(
            rfq,
            workflow_state=PRICING_WORKFLOW_STATE_REJECTED,
            validation_by=current_user.email,
            validation_at=validation_at,
            rejection_reason=rejection_reason,
        )
        rfq.revision_notes = _append_revision_note(
            rfq.revision_notes,
            "Costing pricing rejection: ",
            rejection_reason,
        )
        await log_action(
            db,
            rfq_id,
            f"Pricing file rejected: {rejection_reason}",
            current_user.email,
        )

    await db.commit()
    refreshed_rfq = await _get_rfq_or_404(db, rfq_id)

    if body.is_approved:
        if _is_rfi(refreshed_rfq):
            email_sent = emails.send_rfi_completed_email(
                refreshed_rfq.created_by_email,
                systematic_rfq_id,
                rfq_link,
                _latest_pricing_file_link(refreshed_rfq),
            )
            email_type = EMAIL_RFI_COMPLETED
        else:
            email_sent = emails.send_costing_approved_email(
                refreshed_rfq.created_by_email,
                systematic_rfq_id,
                rfq_link,
            )
            email_type = EMAIL_COSTING_APPROVED
        if email_sent:
            await record_notification_sent(
                db,
                rfq_id=refreshed_rfq.rfq_id,
                recipients=refreshed_rfq.created_by_email,
                email_type=email_type,
            )
    else:
        costing_agent_email = await resolve_product_line_role_email(
            db,
            role=ProductLineRoutingRole.COSTING,
            acronym=refreshed_rfq.product_line_acronym,
        ) or ""
        email_sent = emails.send_costing_rejected_email(
            costing_agent_email,
            refreshed_rfq.created_by_email,
            systematic_rfq_id,
            rfq_link,
            str(body.rejection_reason or ""),
        )
        if email_sent:
            await record_notification_sent(
                db,
                rfq_id=refreshed_rfq.rfq_id,
                recipients=[costing_agent_email, refreshed_rfq.created_by_email],
                email_type=EMAIL_COSTING_REJECTED,
            )

    await _refresh_rfq_response_state(db, refreshed_rfq)
    return refreshed_rfq


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

    if rfq.phase == RfqPhase.RFQ:
        _assert_can_edit_rfq_phase(current_user, rfq)
    elif rfq.phase == RfqPhase.OFFER:
        _assert_can_edit_offer_phase(current_user, rfq)
    elif rfq.phase == RfqPhase.COSTING:
        if current_user.role == UserRole.COSTING_TEAM:
            await _assert_costing_phase_assignment(db, current_user, rfq)
        elif current_user.role == UserRole.PLM:
            await _assert_costing_phase_assignment(db, current_user, rfq, allow_plm=True)
        elif current_user.role != UserRole.OWNER:
            raise HTTPException(
                status_code=403,
                detail="You are not authorized to advance this costing RFQ.",
            )

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
    _assert_document_type_allows_target(rfq, effective_phase, body.target_sub_status)

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
        _set_pricing_workflow_state(
            rfq,
            **_default_pricing_workflow_state(),
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
