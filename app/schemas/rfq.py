from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.rfq import RfqPhase, RfqSubStatus
from app.schemas.potential import PotentialOut


class RfqOut(BaseModel):
    rfq_id: str
    phase: RfqPhase
    sub_status: RfqSubStatus
    product_line_acronym: str | None
    contact_id: int | None
    zone_manager_email: str | None
    created_by_email: str
    rfq_data: dict[str, Any] | None
    chat_history: list[dict[str, Any]] | None
    costing_files: list[dict[str, Any]] | None
    costing_file_state: dict[str, Any] | None
    potential: PotentialOut | None = None
    rejection_reason: str | None
    revision_notes: str | None
    autopsy_notes: str | None
    approved_at: datetime | None
    rejected_at: datetime | None
    last_notification_sent_at: datetime | None = None
    follow_up_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AuditLogOut(BaseModel):
    log_id: str
    rfq_id: str
    action: str
    performed_by: str
    timestamp: datetime

    model_config = {"from_attributes": True}


class NotificationLogOut(BaseModel):
    log_id: str
    rfq_id: str
    recipient_email: str
    email_type: str
    sent_at: datetime

    model_config = {"from_attributes": True}


class RfqDataPayload(BaseModel):
    po_date: str | None = None
    ppap_date: str | None = None

    model_config = ConfigDict(extra="allow")


def rfq_data_payload_to_dict(
    payload: "RfqDataPayload | dict[str, Any] | None",
) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    return payload.model_dump(exclude_unset=True)


class RfqCreateRequest(BaseModel):
    """Optional body when creating a new RFQ.
    chat_mode controls the initial sub_status: 'potential' → POTENTIAL, 'rfq' → NEW_RFQ.
    """
    chat_mode: str = "rfq"
    rfq_data: RfqDataPayload | None = None


class RfqDataUpdateRequest(BaseModel):
    rfq_data: RfqDataPayload


class PhaseStatusUpdateRequest(BaseModel):
    """Direct phase + sub_status update (admin/owner use)."""
    phase: RfqPhase
    sub_status: RfqSubStatus


class AutopsyRequest(BaseModel):
    """Required when an RFQ is in LOST or CANCELED sub_status."""
    rejection_reason: str
    autopsy_notes: str


class ValidateRfqRequest(BaseModel):
    """Body for POST /api/rfq/{id}/validate - Validator approve/reject."""
    approved: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_rejected(self) -> "ValidateRfqRequest":
        if not self.approved and not self.rejection_reason:
            raise ValueError("rejection_reason is required when approved=False")
        return self


class RequestRevisionRequest(BaseModel):
    comment: str


class CostingReviewRequest(BaseModel):
    """Body for POST /api/rfq/{id}/costing_review — Costing scope step."""
    scope: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_out_of_scope(self) -> "CostingReviewRequest":
        if not self.scope and not self.rejection_reason:
            raise ValueError("rejection_reason is required when scope=False")
        return self


class CostingValidationRequest(BaseModel):
    """Body for POST /api/rfq/{id}/costing_validation - Pricing approval step."""
    is_approved: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_rejected(self) -> "CostingValidationRequest":
        if not self.is_approved and not self.rejection_reason:
            raise ValueError("rejection_reason is required when is_approved=False")
        return self


class AdvanceStatusRequest(BaseModel):
    """Advance an RFQ through the state machine."""
    target_phase: RfqPhase
    target_sub_status: RfqSubStatus
    notes: str | None = None
    # Required when transitioning to LOST or CANCELED
    autopsy_notes: str | None = None

    @model_validator(mode="after")
    def autopsy_required_for_terminal(self) -> "AdvanceStatusRequest":
        terminal = {RfqSubStatus.LOST, RfqSubStatus.CANCELED}
        if self.target_sub_status in terminal and not self.autopsy_notes:
            raise ValueError(
                "autopsy_notes is required when transitioning to LOST or CANCELED"
            )
        return self
