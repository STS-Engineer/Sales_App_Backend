import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.discussion import DiscussionMessage
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User, UserRole
from app.routers import rfq as rfq_router
from app.routers.auth import create_access_token


async def _create_user(
    db_session: AsyncSession,
    *,
    prefix: str,
    role: UserRole,
    full_name: str,
) -> User:
    user = User(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@avocarbon.com",
        full_name=full_name,
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
    creator: User,
    zone_manager: User | None = None,
    phase: RfqPhase,
    sub_status: RfqSubStatus,
    costing_file_state: dict | None = None,
    product_line_acronym: str = "BRU",
    product_name: str = "Brushes",
) -> Rfq:
    rfq = Rfq(
        phase=phase,
        sub_status=sub_status,
        product_line_acronym=product_line_acronym,
        zone_manager_email=zone_manager.email if zone_manager else None,
        created_by_email=creator.email,
        rfq_data={
            "product_name": product_name,
            "product_line_acronym": product_line_acronym,
            "systematic_rfq_id": f"26{uuid.uuid4().hex[:3].upper()}-{product_line_acronym}-00",
            "zone_manager_email": zone_manager.email if zone_manager else None,
        },
        chat_history=[],
        costing_file_state=costing_file_state,
    )
    db_session.add(rfq)
    await db_session.commit()
    await db_session.refresh(rfq)
    return rfq


@pytest.mark.asyncio
async def test_validation_approval_initializes_costing_file_state_and_sends_entry_email(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="costing-creator",
        role=UserRole.COMMERCIAL,
        full_name="Costing Creator",
    )
    validator = await _create_user(
        db_session,
        prefix="costing-validator",
        role=UserRole.ZONE_MANAGER,
        full_name="Costing Validator",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        zone_manager=validator,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.PENDING_FOR_VALIDATION,
    )

    email_calls: list[tuple[str, str]] = []

    def _fake_send_costing_entry_email(
        _recipient_email: str,
        _product_line: str,
        _product_code: str,
        systematic_rfq_id: str,
        _rfq_link: str,
    ) -> bool:
        email_calls.append(
            (systematic_rfq_id, "mohamedlaith.benmabrouk@avocarbon.com")
        )
        return True

    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_entry_email",
        _fake_send_costing_entry_email,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/validate",
        json={"approved": True},
        headers=_headers_for(validator),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "COSTING"
    assert payload["sub_status"] == "FEASIBILITY"
    assert payload["costing_file_state"]["file_status"] == "PENDING"
    assert email_calls == [
        (
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            "mohamedlaith.benmabrouk@avocarbon.com",
        )
    ]


@pytest.mark.asyncio
async def test_costing_review_approval_sends_reception_and_handoff_emails(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="costing-review-creator",
        role=UserRole.COMMERCIAL,
        full_name="Review Creator",
    )
    validator = await _create_user(
        db_session,
        prefix="costing-review-validator",
        role=UserRole.ZONE_MANAGER,
        full_name="Review Validator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="costing-review-user",
        role=UserRole.COSTING_TEAM,
        full_name="Costing Reviewer",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        zone_manager=validator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    reception_calls: list[tuple[str, str | None, str, str, bool, str | None]] = []
    handoff_calls: list[tuple[str, str, str]] = []
    sync_calls: list[str] = []

    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_reception_results_email",
        lambda to_email, cc_email, review_user_email, systematic_rfq_id, _rfq_link, is_approved, rejection_reason=None: (
            reception_calls.append(
                (
                    to_email,
                    cc_email,
                    review_user_email,
                    systematic_rfq_id,
                    is_approved,
                    rejection_reason,
                )
            )
            or True
        ),
    )
    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_handoff_email",
        lambda recipient_email, _product_line, product_code, systematic_rfq_id, _rfq_link: (
            handoff_calls.append((systematic_rfq_id, recipient_email, product_code))
            or True
        ),
    )
    async def _fake_sync_rfq_to_assembly(_rfq: Rfq) -> bool:
        sync_calls.append(_rfq.rfq_id)
        return True

    monkeypatch.setattr(rfq_router, "sync_rfq_to_assembly", _fake_sync_rfq_to_assembly)

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing_review",
        json={"scope": True},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 200
    assert reception_calls == [
        (
            creator.email,
            validator.email,
            costing_user.email,
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            True,
            None,
        )
    ]
    assert handoff_calls == [
        (
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            "mohamedlaith.benmabrouk@avocarbon.com",
            "BRU",
        )
    ]
    assert sync_calls == []


@pytest.mark.asyncio
async def test_costing_review_approval_syncs_assembly_rfq_only_for_ass_product_line(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="assembly-sync-creator",
        role=UserRole.COMMERCIAL,
        full_name="Assembly Creator",
    )
    validator = await _create_user(
        db_session,
        prefix="assembly-sync-validator",
        role=UserRole.ZONE_MANAGER,
        full_name="Assembly Validator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="assembly-sync-user",
        role=UserRole.COSTING_TEAM,
        full_name="Assembly Costing Reviewer",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        zone_manager=validator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
        product_line_acronym="ASS",
        product_name="Assembly",
    )

    sync_calls: list[str] = []

    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_reception_results_email",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_handoff_email",
        lambda *args, **kwargs: True,
    )

    async def _fake_sync_rfq_to_assembly(_rfq: Rfq) -> bool:
        sync_calls.append(_rfq.rfq_id)
        return True

    monkeypatch.setattr(rfq_router, "sync_rfq_to_assembly", _fake_sync_rfq_to_assembly)

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing_review",
        json={"scope": True},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 200
    assert sync_calls == [rfq.rfq_id]


@pytest.mark.asyncio
async def test_costing_review_rejection_sends_reception_email_without_handoff(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="costing-reject-creator",
        role=UserRole.COMMERCIAL,
        full_name="Reject Creator",
    )
    validator = await _create_user(
        db_session,
        prefix="costing-reject-validator",
        role=UserRole.ZONE_MANAGER,
        full_name="Reject Validator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="costing-reject-user",
        role=UserRole.COSTING_TEAM,
        full_name="Reject Reviewer",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        zone_manager=validator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    reception_calls: list[tuple[str, str | None, str, str, bool, str | None]] = []
    handoff_calls: list[tuple[str, str, str]] = []
    sync_calls: list[str] = []

    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_reception_results_email",
        lambda to_email, cc_email, review_user_email, systematic_rfq_id, _rfq_link, is_approved, rejection_reason=None: (
            reception_calls.append(
                (
                    to_email,
                    cc_email,
                    review_user_email,
                    systematic_rfq_id,
                    is_approved,
                    rejection_reason,
                )
            )
            or True
        ),
    )
    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_handoff_email",
        lambda recipient_email, _product_line, product_code, systematic_rfq_id, _rfq_link: (
            handoff_calls.append((systematic_rfq_id, recipient_email, product_code))
            or True
        ),
    )
    async def _fake_sync_rfq_to_assembly(_rfq: Rfq) -> bool:
        sync_calls.append(_rfq.rfq_id)
        return True

    monkeypatch.setattr(rfq_router, "sync_rfq_to_assembly", _fake_sync_rfq_to_assembly)

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing_review",
        json={"scope": False, "rejection_reason": "Not feasible for current costing scope."},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 200
    assert response.json()["sub_status"] == "CANCELED"
    assert response.json()["rejection_reason"] == "Not feasible for current costing scope."
    assert reception_calls == [
        (
            creator.email,
            validator.email,
            costing_user.email,
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            False,
            "Not feasible for current costing scope.",
        )
    ]
    assert handoff_calls == []
    assert sync_calls == []


@pytest.mark.asyncio
async def test_costing_file_action_supports_na_and_uploaded(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="costing-file-creator",
        role=UserRole.COMMERCIAL,
        full_name="File Creator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="costing-file-user",
        role=UserRole.COSTING_TEAM,
        full_name="File Reviewer",
    )
    rfq_na = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    na_response = await client.post(
        f"/api/rfq/{rfq_na.rfq_id}/costing-file-action",
        data={"action": "NA", "note": "Handled in the standard costing template."},
        headers=_headers_for(costing_user),
    )

    assert na_response.status_code == 200
    na_payload = na_response.json()
    assert na_payload["costing_file_state"]["file_status"] == "NA"
    assert na_payload["costing_file_state"]["file_note"] == "Handled in the standard costing template."
    assert na_payload["costing_file_state"]["action_by"] == costing_user.email

    rfq_upload = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    async def _fake_upload_costing_action_file(*, rfq_id, file, current_user_email):
        return {
            "id": f"{rfq_id}-file",
            "filename": "feasibility.xlsx",
            "name": "feasibility.xlsx",
            "download_url": "https://example.com/feasibility.xlsx",
            "url": "https://example.com/feasibility.xlsx",
            "uploaded_by": current_user_email,
            "uploaded_at": "2026-04-16T12:00:00+00:00",
        }

    monkeypatch.setattr(
        rfq_router,
        "_upload_costing_action_file",
        _fake_upload_costing_action_file,
    )

    upload_response = await client.post(
        f"/api/rfq/{rfq_upload.rfq_id}/costing-file-action",
        data={"action": "UPLOADED", "note": "Final feasibility workbook attached."},
        files={"file": ("feasibility.xlsx", b"dummy-bytes", "application/vnd.ms-excel")},
        headers=_headers_for(costing_user),
    )

    assert upload_response.status_code == 200
    upload_payload = upload_response.json()
    assert upload_payload["costing_file_state"]["file_status"] == "UPLOADED"
    assert upload_payload["costing_file_state"]["file"]["filename"] == "feasibility.xlsx"
    assert upload_payload["costing_files"][-1]["filename"] == "feasibility.xlsx"


@pytest.mark.asyncio
async def test_advance_to_pricing_requires_costing_file_action(
    client: AsyncClient,
    db_session: AsyncSession,
):
    creator = await _create_user(
        db_session,
        prefix="costing-advance-creator",
        role=UserRole.COMMERCIAL,
        full_name="Advance Creator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="costing-advance-user",
        role=UserRole.COSTING_TEAM,
        full_name="Advance Reviewer",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )
    db_session.add(
        AuditLog(
            rfq_id=rfq.rfq_id,
            action="Costing review approved",
            performed_by=costing_user.email,
        )
    )
    await db_session.commit()

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/advance",
        json={"target_phase": "COSTING", "target_sub_status": "PRICING"},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 400
    assert "Complete the costing file action first" in response.json()["detail"]


@pytest.mark.asyncio
async def test_costing_messages_store_recipient_and_unify_costing_thread(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="costing-msg-creator",
        role=UserRole.COMMERCIAL,
        full_name="Message Creator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="costing-msg-user",
        role=UserRole.COSTING_TEAM,
        full_name="Message Sender",
    )
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.PRICING,
        costing_file_state={"file_status": "UPLOADED"},
    )

    email_calls: list[tuple[str, str, str, str]] = []

    monkeypatch.setattr(
        rfq_router.emails,
        "send_costing_message_email",
        lambda recipient_email, systematic_rfq_id, sender_email, message, _rfq_link: (
            email_calls.append(
                (recipient_email, systematic_rfq_id, sender_email, message)
            )
            or True
        ),
    )

    post_response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing-messages",
        json={
            "message": "Please confirm the pricing assumptions.",
            "recipient_email": "pricing.owner@avocarbon.com",
        },
        headers=_headers_for(costing_user),
    )

    assert post_response.status_code == 201
    posted_payload = post_response.json()
    assert posted_payload["phase"] == "PRICING"
    assert posted_payload["recipient_email"] == "pricing.owner@avocarbon.com"
    assert email_calls == [
        (
            "pricing.owner@avocarbon.com",
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            costing_user.email,
            "Please confirm the pricing assumptions.",
        )
    ]

    db_session.add(
        DiscussionMessage(
            rfq_id=rfq.rfq_id,
            user_id=creator.user_id,
            phase=RfqSubStatus.FEASIBILITY,
            message="Initial feasibility note",
            recipient_email="team@avocarbon.com",
        )
    )
    await db_session.commit()

    get_response = await client.get(
        f"/api/rfq/{rfq.rfq_id}/costing-messages",
        headers=_headers_for(costing_user),
    )

    assert get_response.status_code == 200
    payload = get_response.json()
    assert len(payload) == 2
    assert {item["phase"] for item in payload} == {"FEASIBILITY", "PRICING"}
    assert any(item["recipient_email"] == "pricing.owner@avocarbon.com" for item in payload)

    stored = await db_session.execute(
        select(DiscussionMessage).where(
            DiscussionMessage.rfq_id == rfq.rfq_id,
            DiscussionMessage.recipient_email == "pricing.owner@avocarbon.com",
        )
    )
    assert stored.scalar_one().phase == RfqSubStatus.PRICING
