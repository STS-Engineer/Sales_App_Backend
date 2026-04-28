import datetime
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification_log import NotificationLog
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User, UserRole
from app.models.validation_matrix import ValidationMatrix
from app.routers import internal
from app.routers.auth import create_access_token
from app.services.notifications import EMAIL_SLA_REMINDER
from app.tasks.followups import run_followup_sweep


async def _ensure_matrix(db_session: AsyncSession, acronym: str = "BRU") -> None:
    existing = (
        await db_session.execute(
            select(ValidationMatrix).where(ValidationMatrix.acronym == acronym)
        )
    ).scalar_one_or_none()
    if existing:
        return
    db_session.add(
        ValidationMatrix(
            product_line=f"Matrix {acronym}",
            acronym=acronym,
            n3_kam_limit=10,
            n2_zone_limit=20,
            n1_vp_limit=30,
        )
    )
    await db_session.commit()


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    role: UserRole = UserRole.COMMERCIAL,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=f"{prefix.title()} User",
        role=role,
        is_approved=True,
    )
    user.set_password("secure-password")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _headers_for(user: User) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(user.email, user.role.value)}"
    }


async def _create_rfq(
    db_session: AsyncSession,
    *,
    phase: RfqPhase,
    sub_status: RfqSubStatus,
    product_line_acronym: str = "BRU",
    zone_manager_email: str | None = None,
    costing_file_state: dict | None = None,
    updated_at: datetime.datetime | None = None,
) -> Rfq:
    await _ensure_matrix(db_session, product_line_acronym)
    creator = await _create_user(db_session, prefix="followup-creator")
    rfq = Rfq(
        phase=phase,
        sub_status=sub_status,
        product_line_acronym=product_line_acronym,
        zone_manager_email=zone_manager_email,
        created_by_email=creator.email,
        rfq_data={
            "systematic_rfq_id": f"26{uuid.uuid4().hex[:3].upper()}-{product_line_acronym}-00",
            "product_line_acronym": product_line_acronym,
        },
        chat_history=[],
        costing_file_state=costing_file_state,
        updated_at=updated_at,
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)
    return rfq


@pytest.mark.asyncio
async def test_followup_sweep_sends_validation_reminder_and_logs(
    db_session: AsyncSession,
    monkeypatch,
):
    stale_at = datetime.datetime.utcnow() - datetime.timedelta(days=3)
    rfq = await _create_rfq(
        db_session,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
        zone_manager_email="validator@example.com",
        updated_at=stale_at,
    )
    captured: dict[str, object] = {}

    def _fake_followup(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(
        "app.tasks.followups.emails.send_action_required_followup",
        _fake_followup,
    )

    summary = await run_followup_sweep(db_session)

    await db_session.refresh(rfq)
    logs = (
        await db_session.execute(
            select(NotificationLog).where(NotificationLog.rfq_id == rfq.rfq_id)
        )
    ).scalars().all()
    assert summary["sent"] == 1
    assert captured["recipient_email"] == "validator@example.com"
    assert captured["action_description"] == "Commercial Validation"
    assert rfq.follow_up_count == 1
    assert rfq.last_notification_sent_at is not None
    assert len(logs) == 1
    assert logs[0].email_type == EMAIL_SLA_REMINDER


@pytest.mark.asyncio
async def test_followup_sweep_sends_rnd_and_bom_reminders(
    db_session: AsyncSession,
    monkeypatch,
):
    stale_at = datetime.datetime.utcnow() - datetime.timedelta(days=4)
    await _create_rfq(
        db_session,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
        updated_at=stale_at,
    )
    await _create_rfq(
        db_session,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.PRICING,
        costing_file_state={"workflow_state": "WAITING_BOM"},
        updated_at=stale_at,
    )
    actions: list[str] = []

    def _fake_followup(**kwargs):
        actions.append(str(kwargs["action_description"]))
        return True

    monkeypatch.setattr(
        "app.tasks.followups.emails.send_action_required_followup",
        _fake_followup,
    )

    summary = await run_followup_sweep(db_session)

    assert summary["sent"] == 2
    assert sorted(actions) == ["BOM Upload", "R&D Feasibility Assessment"]


@pytest.mark.asyncio
async def test_followup_sweep_skips_fresh_terminal_missing_blocker_and_failed_email(
    db_session: AsyncSession,
    monkeypatch,
):
    now = datetime.datetime.utcnow()
    stale_at = now - datetime.timedelta(days=3)
    await _create_rfq(
        db_session,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
        zone_manager_email="",
        updated_at=stale_at,
    )
    await _create_rfq(
        db_session,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.CANCELED,
        zone_manager_email="validator@example.com",
        updated_at=stale_at,
    )
    await _create_rfq(
        db_session,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
        zone_manager_email="fresh@example.com",
        updated_at=now,
    )
    failed_rfq = await _create_rfq(
        db_session,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
        zone_manager_email="fail@example.com",
        updated_at=stale_at,
    )

    monkeypatch.setattr(
        "app.tasks.followups.emails.send_action_required_followup",
        lambda **kwargs: False,
    )

    summary = await run_followup_sweep(db_session)

    await db_session.refresh(failed_rfq)
    logs = (
        await db_session.execute(
            select(NotificationLog).where(NotificationLog.rfq_id == failed_rfq.rfq_id)
        )
    ).scalars().all()
    assert summary["sent"] == 0
    assert summary["failed"] == 1
    assert summary["skipped"] == 2
    assert failed_rfq.follow_up_count == 0
    assert failed_rfq.last_notification_sent_at is None
    assert logs == []


@pytest.mark.asyncio
async def test_trigger_followups_requires_token_and_runs_sweep(
    client: AsyncClient,
    monkeypatch,
):
    monkeypatch.setattr(settings, "CRON_TOKEN", "secret-token", raising=False)

    async def _fake_sweep(db):
        return {"scanned": 1, "sent": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr(internal, "run_followup_sweep", _fake_sweep)

    missing = await client.post("/api/internal/trigger-followups")
    wrong = await client.post(
        "/api/internal/trigger-followups",
        headers={"X-Cron-Token": "wrong"},
    )
    ok = await client.post(
        "/api/internal/trigger-followups",
        headers={"X-Cron-Token": "secret-token"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert ok.status_code == 200
    assert ok.json()["sent"] == 1


@pytest.mark.asyncio
async def test_notification_endpoint_returns_logs_for_visible_rfq(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(db_session, prefix="notification-viewer")
    await _ensure_matrix(db_session)
    rfq = Rfq(
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.NEW_RFQ,
        product_line_acronym="BRU",
        created_by_email=creator.email,
        rfq_data={"systematic_rfq_id": "26001-BRU-00"},
        chat_history=[],
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)

    old_log = NotificationLog(
        rfq_id=rfq.rfq_id,
        recipient_email="first@example.com",
        email_type=EMAIL_SLA_REMINDER,
        sent_at=datetime.datetime.utcnow() - datetime.timedelta(days=1),
    )
    new_log = NotificationLog(
        rfq_id=rfq.rfq_id,
        recipient_email="second@example.com",
        email_type=EMAIL_SLA_REMINDER,
        sent_at=datetime.datetime.utcnow(),
    )
    db_session.add_all([old_log, new_log])
    await db_session.commit()

    response = await client.get(
        f"/api/rfq/{rfq.rfq_id}/notifications",
        headers=_headers_for(creator),
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["recipient_email"] for item in payload] == [
        "second@example.com",
        "first@example.com",
    ]
