import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.routers.rfq import (
    _build_rfq_link,
    _effective_pricing_workflow_state,
    get_costing_agent_email,
    get_rnd_email,
)
from app.services.notifications import (
    EMAIL_SLA_REMINDER,
    add_notification_logs,
)
from app.utils import emails

logger = logging.getLogger(__name__)

FOLLOWUP_THRESHOLD = datetime.timedelta(hours=48)
TERMINAL_SUBSTATUSES = {RfqSubStatus.LOST, RfqSubStatus.CANCELED, RfqSubStatus.PO_SECURED}


def _as_aware_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _days_pending(reference: datetime.datetime | None, now: datetime.datetime) -> int:
    normalized_now = _as_aware_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    normalized_reference = _as_aware_utc(reference) or normalized_now
    return max((normalized_now - normalized_reference).days, 0)


def _systematic_rfq_id(rfq: Rfq) -> str:
    rfq_data = dict(rfq.rfq_data or {})
    return str(rfq_data.get("systematic_rfq_id") or "").strip()


def _resolve_followup_blocker(rfq: Rfq) -> tuple[str | None, str | None]:
    if rfq.phase == RfqPhase.RFQ and rfq.sub_status == RfqSubStatus.PENDING_FOR_VALIDATION:
        return rfq.zone_manager_email, "Commercial Validation"

    if rfq.phase == RfqPhase.COSTING and rfq.sub_status == RfqSubStatus.FEASIBILITY:
        state = dict(rfq.costing_file_state or {})
        if not str(state.get("feasibility_status") or "").strip():
            return get_rnd_email(rfq.product_line_acronym or ""), "R&D Feasibility Assessment"

    if rfq.phase == RfqPhase.COSTING and rfq.sub_status == RfqSubStatus.PRICING:
        pricing_state = _effective_pricing_workflow_state(rfq)
        if not isinstance(pricing_state.get("bom_file"), dict):
            return get_costing_agent_email(rfq.product_line_acronym or ""), "BOM Upload"

    return None, None


async def run_followup_sweep(db: AsyncSession) -> dict[str, int]:
    now = datetime.datetime.utcnow()
    cutoff = now - FOLLOWUP_THRESHOLD
    summary = {"scanned": 0, "sent": 0, "skipped": 0, "failed": 0}

    result = await db.execute(
        select(Rfq)
        .where(func.coalesce(Rfq.last_notification_sent_at, Rfq.updated_at) < cutoff)
        .order_by(Rfq.updated_at.asc())
    )
    rfqs = result.scalars().all()
    summary["scanned"] = len(rfqs)

    for rfq in rfqs:
        if rfq.phase == RfqPhase.CLOSED or rfq.sub_status in TERMINAL_SUBSTATUSES:
            summary["skipped"] += 1
            continue

        recipient_email, action_description = _resolve_followup_blocker(rfq)
        if not recipient_email or not action_description:
            summary["skipped"] += 1
            continue

        reference_time = rfq.last_notification_sent_at or rfq.updated_at
        days_pending = _days_pending(reference_time, now)
        try:
            email_sent = emails.send_action_required_followup(
                recipient_email=recipient_email,
                systematic_rfq_id=_systematic_rfq_id(rfq),
                action_description=action_description,
                days_pending=days_pending,
                rfq_link=_build_rfq_link(rfq.rfq_id),
            )
            if not email_sent:
                summary["failed"] += 1
                continue

            rfq.last_notification_sent_at = now
            rfq.follow_up_count = int(rfq.follow_up_count or 0) + 1
            await add_notification_logs(
                db,
                rfq_id=rfq.rfq_id,
                recipients=recipient_email,
                email_type=EMAIL_SLA_REMINDER,
                sent_at=now,
            )
            await db.commit()
            summary["sent"] += 1
        except Exception:
            await db.rollback()
            summary["failed"] += 1
            logger.exception("Failed to process SLA follow-up for RFQ %s.", rfq.rfq_id)

    return summary
