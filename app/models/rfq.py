import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# ── Phase enum ────────────────────────────────────────────────────────
class RfqPhase(str, enum.Enum):
    RFQ = "RFQ"
    COSTING = "COSTING"
    OFFER = "OFFER"
    PO = "PO"
    PROTOTYPE = "PROTOTYPE"
    CLOSED = "CLOSED"


# ── Sub-status enum ──────────────────────────────────────────────────
class RfqSubStatus(str, enum.Enum):
    # RFQ phase
    POTENTIAL = "POTENTIAL"
    NEW_RFQ = "NEW_RFQ"
    IN_VALIDATION = "IN_VALIDATION"
    # COSTING phase
    FEASIBILITY = "FEASIBILITY"
    PRICING = "PRICING"
    # OFFER phase
    PREPARATION = "PREPARATION"
    VALIDATION = "VALIDATION"
    # PO phase
    GET_PO = "GET_PO"
    PO_ACCEPTED = "PO_ACCEPTED"
    MISSION_ACCEPTED = "MISSION_ACCEPTED"
    MISSION_NOT_ACCEPTED = "MISSION_NOT_ACCEPTED"
    # PROTOTYPE phase
    GET_PROTOTYPE = "GET_PROTOTYPE"
    PROTOTYPE_ONGOING = "PROTOTYPE_ONGOING"
    # Global negative terminal
    LOST = "LOST"
    CANCELED = "CANCELED"
    # Terminal positive (under CLOSED phase)
    PO_SECURED = "PO_SECURED"


# ── Valid (phase, sub_status) pairs ──────────────────────────────────
VALID_PHASE_SUBSTATUS: dict[RfqPhase, set[RfqSubStatus]] = {
    RfqPhase.RFQ: {
        RfqSubStatus.POTENTIAL,
        RfqSubStatus.NEW_RFQ,
        RfqSubStatus.IN_VALIDATION,
    },
    RfqPhase.COSTING: {
        RfqSubStatus.FEASIBILITY,
        RfqSubStatus.PRICING,
    },
    RfqPhase.OFFER: {
        RfqSubStatus.PREPARATION,
        RfqSubStatus.VALIDATION,
    },
    RfqPhase.PO: {
        RfqSubStatus.GET_PO,
        RfqSubStatus.PO_ACCEPTED,
        RfqSubStatus.MISSION_ACCEPTED,
        RfqSubStatus.MISSION_NOT_ACCEPTED,
    },
    RfqPhase.PROTOTYPE: {
        RfqSubStatus.GET_PROTOTYPE,
        RfqSubStatus.PROTOTYPE_ONGOING,
    },
    RfqPhase.CLOSED: {
        RfqSubStatus.PO_SECURED,
        RfqSubStatus.LOST,
        RfqSubStatus.CANCELED,
    },
}


AUTOPSY_REQUIRED_SUBSTATUSES: set[RfqSubStatus] = {
    RfqSubStatus.LOST,
    RfqSubStatus.CANCELED,
}

# ── Allowed forward transitions: (from) → {(to), ...} ───────────────
ALLOWED_TRANSITIONS: dict[
    tuple[RfqPhase, RfqSubStatus],
    set[tuple[RfqPhase, RfqSubStatus]],
] = {
    # ── RFQ phase ────────────────────────────────────────────────────
    (RfqPhase.RFQ, RfqSubStatus.POTENTIAL): {
        (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ): {
        (RfqPhase.RFQ, RfqSubStatus.IN_VALIDATION),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    (RfqPhase.RFQ, RfqSubStatus.IN_VALIDATION): {
        (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY),  # approved
        (RfqPhase.CLOSED, RfqSubStatus.LOST),           # rejected
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    # ── COSTING phase ────────────────────────────────────────────────
    (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY): {
        (RfqPhase.COSTING, RfqSubStatus.PRICING),       # feasible
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    (RfqPhase.COSTING, RfqSubStatus.PRICING): {
        (RfqPhase.OFFER, RfqSubStatus.PREPARATION),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    # ── OFFER phase ──────────────────────────────────────────────────
    (RfqPhase.OFFER, RfqSubStatus.PREPARATION): {
        (RfqPhase.OFFER, RfqSubStatus.VALIDATION),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    (RfqPhase.OFFER, RfqSubStatus.VALIDATION): {
        (RfqPhase.PO, RfqSubStatus.GET_PO),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    # ── PO phase ─────────────────────────────────────────────────────
    (RfqPhase.PO, RfqSubStatus.GET_PO): {
        (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED),
        (RfqPhase.PROTOTYPE, RfqSubStatus.GET_PROTOTYPE),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED): {
        (RfqPhase.PO, RfqSubStatus.MISSION_ACCEPTED),
        (RfqPhase.PO, RfqSubStatus.MISSION_NOT_ACCEPTED),
    },
    (RfqPhase.PO, RfqSubStatus.MISSION_ACCEPTED): {
        (RfqPhase.CLOSED, RfqSubStatus.PO_SECURED),
    },
    # MISSION_NOT_ACCEPTED → auto-locks to LOST/CANCELED
    (RfqPhase.PO, RfqSubStatus.MISSION_NOT_ACCEPTED): {
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    # ── PROTOTYPE phase ──────────────────────────────────────────────
    (RfqPhase.PROTOTYPE, RfqSubStatus.GET_PROTOTYPE): {
        (RfqPhase.PROTOTYPE, RfqSubStatus.PROTOTYPE_ONGOING),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
    (RfqPhase.PROTOTYPE, RfqSubStatus.PROTOTYPE_ONGOING): {
        (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED),
        (RfqPhase.CLOSED, RfqSubStatus.PO_SECURED),
        (RfqPhase.CLOSED, RfqSubStatus.LOST),
        (RfqPhase.CLOSED, RfqSubStatus.CANCELED),
    },
}


class Rfq(Base):
    __tablename__ = "rfq"

    # The UUID remains the durable primary key.
    # The user-facing formatted ID is stored in rfq_data["systematic_rfq_id"].
    rfq_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ── New phase / sub-status columns ────────────────────────────────
    phase: Mapped[RfqPhase] = mapped_column(
        SAEnum(RfqPhase, name="rfqphase"),
        nullable=False,
        default=RfqPhase.RFQ,
    )
    sub_status: Mapped[RfqSubStatus] = mapped_column(
        SAEnum(RfqSubStatus, name="rfqsubstatus"),
        nullable=False,
        default=RfqSubStatus.POTENTIAL,
    )

    # FK to validation_matrix.acronym (unique-constrained)
    product_line_acronym: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("validation_matrix.acronym", ondelete="RESTRICT"),
        nullable=True,
    )
    contact_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("contacts.contact_id", ondelete="SET NULL"), nullable=True
    )
    zone_manager_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_email: Mapped[str] = mapped_column(
        String, ForeignKey("users.email", ondelete="RESTRICT"), nullable=False
    )
    # All structured RFQ data collected by the chatbot
    rfq_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Full conversation history
    chat_history: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Set only when sub_status = LOST / CANCELED
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    autopsy_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Files uploaded by the costing team
    costing_files: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    @property
    def requires_autopsy_report(self) -> bool:
        return self.sub_status in AUTOPSY_REQUIRED_SUBSTATUSES
