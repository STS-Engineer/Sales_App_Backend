from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.models.rfq import RfqPhase, RfqSubStatus


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
    rejection_reason: str | None
    autopsy_notes: str | None
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


class RfqCreateRequest(BaseModel):
    """Optional body when creating a new RFQ.
    chat_mode controls the initial sub_status: 'potential' → POTENTIAL, 'rfq' → NEW_RFQ.
    """
    chat_mode: str = "rfq"
    rfq_data: dict[str, Any] | None = None


class RfqDataUpdateRequest(BaseModel):
    rfq_data: dict[str, Any]


class PhaseStatusUpdateRequest(BaseModel):
    """Direct phase + sub_status update (admin/owner use)."""
    phase: RfqPhase
    sub_status: RfqSubStatus


class AutopsyRequest(BaseModel):
    """Required when an RFQ is in LOST or CANCELED sub_status."""
    rejection_reason: str
    autopsy_notes: str


class ValidateRfqRequest(BaseModel):
    """Body for POST /api/rfq/{id}/validate — Zone Manager approve/reject."""
    approved: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_rejected(self) -> "ValidateRfqRequest":
        if not self.approved and not self.rejection_reason:
            raise ValueError("rejection_reason is required when approved=False")
        return self


class CostingReviewRequest(BaseModel):
    """Body for POST /api/rfq/{id}/costing_review — Costing scope step."""
    scope: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_out_of_scope(self) -> "CostingReviewRequest":
        if not self.scope and not self.rejection_reason:
            raise ValueError("rejection_reason is required when scope=False")
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
