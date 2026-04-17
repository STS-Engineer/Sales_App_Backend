import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.potential import Potential


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
    PENDING_FOR_VALIDATION = "PENDING_FOR_VALIDATION"
    REVISION_REQUESTED = "REVISION_REQUESTED"
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


# ── Terminal sub-statuses that can occur in any phase ────────────────
_TERMINAL_ANYWHERE: set[RfqSubStatus] = {
    RfqSubStatus.LOST,
    RfqSubStatus.CANCELED,
}

# ── Valid (phase, sub_status) pairs ──────────────────────────────────
VALID_PHASE_SUBSTATUS: dict[RfqPhase, set[RfqSubStatus]] = {
    RfqPhase.RFQ: {
        RfqSubStatus.POTENTIAL,
        RfqSubStatus.NEW_RFQ,
        RfqSubStatus.PENDING_FOR_VALIDATION,
        RfqSubStatus.REVISION_REQUESTED,
    } | _TERMINAL_ANYWHERE,
    RfqPhase.COSTING: {
        RfqSubStatus.FEASIBILITY,
        RfqSubStatus.PRICING,
    } | _TERMINAL_ANYWHERE,
    RfqPhase.OFFER: {
        RfqSubStatus.PREPARATION,
        RfqSubStatus.VALIDATION,
    } | _TERMINAL_ANYWHERE,
    RfqPhase.PO: {
        RfqSubStatus.GET_PO,
        RfqSubStatus.PO_ACCEPTED,
        RfqSubStatus.MISSION_ACCEPTED,
        RfqSubStatus.MISSION_NOT_ACCEPTED,
    } | _TERMINAL_ANYWHERE,
    RfqPhase.PROTOTYPE: {
        RfqSubStatus.GET_PROTOTYPE,
        RfqSubStatus.PROTOTYPE_ONGOING,
    } | _TERMINAL_ANYWHERE,
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
        (RfqPhase.RFQ, RfqSubStatus.CANCELED),
    },
    (RfqPhase.RFQ, RfqSubStatus.NEW_RFQ): {
        (RfqPhase.RFQ, RfqSubStatus.PENDING_FOR_VALIDATION),
        (RfqPhase.RFQ, RfqSubStatus.CANCELED),
    },
    (RfqPhase.RFQ, RfqSubStatus.PENDING_FOR_VALIDATION): {
        (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY),  # approved
        (RfqPhase.RFQ, RfqSubStatus.CANCELED),
    },
    (RfqPhase.RFQ, RfqSubStatus.REVISION_REQUESTED): {
        (RfqPhase.RFQ, RfqSubStatus.PENDING_FOR_VALIDATION),
        (RfqPhase.RFQ, RfqSubStatus.CANCELED),
    },
    # ── COSTING phase ────────────────────────────────────────────────
    (RfqPhase.COSTING, RfqSubStatus.FEASIBILITY): {
        (RfqPhase.COSTING, RfqSubStatus.PRICING),       # feasible
        (RfqPhase.COSTING, RfqSubStatus.CANCELED),
    },
    (RfqPhase.COSTING, RfqSubStatus.PRICING): {
        (RfqPhase.OFFER, RfqSubStatus.PREPARATION),
        (RfqPhase.COSTING, RfqSubStatus.CANCELED),
    },
    # ── OFFER phase ──────────────────────────────────────────────────
    (RfqPhase.OFFER, RfqSubStatus.PREPARATION): {
        (RfqPhase.OFFER, RfqSubStatus.VALIDATION),
        (RfqPhase.OFFER, RfqSubStatus.LOST),
        (RfqPhase.OFFER, RfqSubStatus.CANCELED),
    },
    (RfqPhase.OFFER, RfqSubStatus.VALIDATION): {
        (RfqPhase.PO, RfqSubStatus.GET_PO),
        (RfqPhase.OFFER, RfqSubStatus.LOST),
        (RfqPhase.OFFER, RfqSubStatus.CANCELED),
    },
    # ── PO phase ─────────────────────────────────────────────────────
    (RfqPhase.PO, RfqSubStatus.GET_PO): {
        (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED),
        (RfqPhase.PROTOTYPE, RfqSubStatus.GET_PROTOTYPE),
        (RfqPhase.PO, RfqSubStatus.LOST),
        (RfqPhase.PO, RfqSubStatus.CANCELED),
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
        (RfqPhase.PO, RfqSubStatus.LOST),
        (RfqPhase.PO, RfqSubStatus.CANCELED),
    },
    # ── PROTOTYPE phase ──────────────────────────────────────────────
    (RfqPhase.PROTOTYPE, RfqSubStatus.GET_PROTOTYPE): {
        (RfqPhase.PROTOTYPE, RfqSubStatus.PROTOTYPE_ONGOING),
        (RfqPhase.PROTOTYPE, RfqSubStatus.LOST),
        (RfqPhase.PROTOTYPE, RfqSubStatus.CANCELED),
    },
    (RfqPhase.PROTOTYPE, RfqSubStatus.PROTOTYPE_ONGOING): {
        (RfqPhase.PO, RfqSubStatus.PO_ACCEPTED),
        (RfqPhase.CLOSED, RfqSubStatus.PO_SECURED),
        (RfqPhase.PROTOTYPE, RfqSubStatus.LOST),
        (RfqPhase.PROTOTYPE, RfqSubStatus.CANCELED),
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
    revision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    autopsy_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Files uploaded by the costing team
    costing_files: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    costing_file_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    potential: Mapped["Potential | None"] = relationship(
        back_populates="rfq",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def requires_autopsy_report(self) -> bool:
        return self.sub_status in AUTOPSY_REQUIRED_SUBSTATUSES
