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


def _assign_matrix_contacts(
    monkeypatch,
    *,
    product_line_acronym: str = "BRU",
    costing_email: str | None = None,
    rnd_email: str | None = None,
    plm_email: str | None = None,
) -> None:
    next_matrix = {
        product_line: dict(entry)
        for product_line, entry in rfq_router.PRODUCT_LINE_MATRIX.items()
    }
    normalized_acronym = str(product_line_acronym or "").strip().upper()

    for entry in next_matrix.values():
        if str(entry.get("code") or "").strip().upper() != normalized_acronym:
            continue
        if costing_email is not None:
            entry["costing_agent_email"] = costing_email
        if rnd_email is not None:
            entry["rnd_email"] = rnd_email
        if plm_email is not None:
            entry["plm_email"] = plm_email
        break

    monkeypatch.setattr(rfq_router, "PRODUCT_LINE_MATRIX", next_matrix)


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
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
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
    _assign_matrix_contacts(
        monkeypatch,
        product_line_acronym="ASS",
        costing_email=costing_user.email,
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
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
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
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
    feasibility_email_calls: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        rfq_router.emails,
        "send_feasibility_result_email",
        lambda recipient_email, systematic_rfq_id, feasibility_status, rfq_link: (
            feasibility_email_calls.append(
                (
                    recipient_email,
                    systematic_rfq_id,
                    feasibility_status,
                    rfq_link,
                )
            )
            or True
        ),
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
        data={
            "action": "NA",
            "note": "Handled in the standard costing template.",
            "feasibility_status": "FEASIBLE_UNDER_CONDITION",
        },
        headers=_headers_for(costing_user),
    )

    assert na_response.status_code == 200
    na_payload = na_response.json()
    assert na_payload["costing_file_state"]["file_status"] == "NA"
    assert na_payload["costing_file_state"]["file_note"] == "Handled in the standard costing template."
    assert na_payload["costing_file_state"]["feasibility_status"] == "FEASIBLE_UNDER_CONDITION"
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
        data={
            "action": "UPLOADED",
            "note": "Final feasibility workbook attached.",
            "feasibility_status": "FEASIBLE",
        },
        files={"file": ("feasibility.xlsx", b"dummy-bytes", "application/vnd.ms-excel")},
        headers=_headers_for(costing_user),
    )

    assert upload_response.status_code == 200
    upload_payload = upload_response.json()
    assert upload_payload["costing_file_state"]["file_status"] == "UPLOADED"
    assert upload_payload["costing_file_state"]["feasibility_status"] == "FEASIBLE"
    assert upload_payload["costing_file_state"]["file"]["filename"] == "feasibility.xlsx"
    assert upload_payload["costing_files"][-1]["filename"] == "feasibility.xlsx"
    assert feasibility_email_calls == []


@pytest.mark.asyncio
async def test_rnd_can_submit_feasibility_file_action_with_status(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="rnd-file-creator",
        role=UserRole.COMMERCIAL,
        full_name="RND File Creator",
    )
    rnd_user = await _create_user(
        db_session,
        prefix="rnd-file-user",
        role=UserRole.RND,
        full_name="RND Engineer",
    )
    _assign_matrix_contacts(monkeypatch, rnd_email=rnd_user.email)
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    async def _fake_upload_costing_action_file(*, rfq_id, file, current_user_email):
        return {
            "id": f"{rfq_id}-rnd-feasibility",
            "filename": "rnd-feasibility.xlsx",
            "name": "rnd-feasibility.xlsx",
            "download_url": "https://example.com/rnd-feasibility.xlsx",
            "url": "https://example.com/rnd-feasibility.xlsx",
            "uploaded_by": current_user_email,
            "uploaded_at": "2026-04-16T14:00:00+00:00",
        }

    monkeypatch.setattr(
        rfq_router,
        "_upload_costing_action_file",
        _fake_upload_costing_action_file,
    )
    feasibility_email_calls: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        rfq_router.emails,
        "send_feasibility_result_email",
        lambda recipient_email, systematic_rfq_id, feasibility_status, rfq_link: (
            feasibility_email_calls.append(
                (
                    recipient_email,
                    systematic_rfq_id,
                    feasibility_status,
                    rfq_link,
                )
            )
            or True
        ),
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing-file-action",
        data={
            "action": "UPLOADED",
            "note": "R&D feasibility package attached.",
            "feasibility_status": "NOT_FEASIBLE",
        },
        files={"file": ("rnd-feasibility.xlsx", b"dummy-bytes", "application/vnd.ms-excel")},
        headers=_headers_for(rnd_user),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["costing_file_state"]["action_by"] == rnd_user.email
    assert payload["costing_file_state"]["feasibility_status"] == "NOT_FEASIBLE"
    assert payload["costing_file_state"]["file"]["filename"] == "rnd-feasibility.xlsx"
    assert feasibility_email_calls == [
        (
            creator.email,
            str((rfq.rfq_data or {}).get("systematic_rfq_id") or ""),
            "NOT_FEASIBLE",
            rfq_router._build_rfq_link(rfq.rfq_id),
        )
    ]


@pytest.mark.asyncio
async def test_costing_file_action_rejects_invalid_feasibility_status(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="costing-file-invalid-creator",
        role=UserRole.COMMERCIAL,
        full_name="Invalid Status Creator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="costing-file-invalid-user",
        role=UserRole.COSTING_TEAM,
        full_name="Invalid Status Reviewer",
    )
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing-file-action",
        data={
            "action": "NA",
            "note": "This should fail.",
            "feasibility_status": "MAYBE",
        },
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 400
    assert "feasibility_status" in response.json()["detail"]


@pytest.mark.asyncio
async def test_unassigned_rnd_cannot_submit_feasibility_file_action(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="rnd-unassigned-creator",
        role=UserRole.COMMERCIAL,
        full_name="Unassigned RND Creator",
    )
    assigned_rnd_user = await _create_user(
        db_session,
        prefix="rnd-assigned",
        role=UserRole.RND,
        full_name="Assigned RND Engineer",
    )
    rnd_user = await _create_user(
        db_session,
        prefix="rnd-unassigned",
        role=UserRole.RND,
        full_name="Unassigned RND Engineer",
    )
    _assign_matrix_contacts(monkeypatch, rnd_email=assigned_rnd_user.email)
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing-file-action",
        data={
            "action": "NA",
            "note": "R&D is not assigned here.",
            "feasibility_status": "FEASIBLE",
        },
        headers=_headers_for(rnd_user),
    )

    assert response.status_code == 403
    assert "R&D" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "restrict_to_costing_phase"),
    [
        (UserRole.COSTING_TEAM, False),
        (UserRole.RND, True),
        (UserRole.PLM, False),
    ],
)
async def test_list_rfqs_filters_assigned_product_lines_for_matrix_roles(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
    role: UserRole,
    restrict_to_costing_phase: bool,
):
    matrix_user = await _create_user(
        db_session,
        prefix=f"matrix-{role.value.lower()}",
        role=role,
        full_name=f"{role.value} Matrix User",
    )
    creator = await _create_user(
        db_session,
        prefix=f"matrix-{role.value.lower()}-creator",
        role=UserRole.COMMERCIAL,
        full_name="Matrix Creator",
    )

    assign_kwargs = {}
    if role == UserRole.COSTING_TEAM:
        assign_kwargs["costing_email"] = matrix_user.email
    elif role == UserRole.RND:
        assign_kwargs["rnd_email"] = matrix_user.email
    else:
        assign_kwargs["plm_email"] = matrix_user.email

    _assign_matrix_contacts(monkeypatch, product_line_acronym="BRU", **assign_kwargs)

    assigned_costing_rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
        product_line_acronym="BRU",
        product_name="Brushes",
    )
    assigned_rfq_stage_rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.RFQ,
        sub_status=RfqSubStatus.NEW_RFQ,
        product_line_acronym="BRU",
        product_name="Brushes",
    )
    other_product_line_rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
        product_line_acronym="SEA",
        product_name="Seals",
    )

    response = await client.get("/api/rfq", headers=_headers_for(matrix_user))

    assert response.status_code == 200
    returned_ids = {item["rfq_id"] for item in response.json()}
    assert assigned_costing_rfq.rfq_id in returned_ids
    assert other_product_line_rfq.rfq_id not in returned_ids
    if restrict_to_costing_phase:
        assert assigned_rfq_stage_rfq.rfq_id not in returned_ids
    else:
        assert assigned_rfq_stage_rfq.rfq_id in returned_ids


@pytest.mark.asyncio
async def test_unassigned_costing_user_cannot_run_costing_review(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="unassigned-costing-creator",
        role=UserRole.COMMERCIAL,
        full_name="Unassigned Costing Creator",
    )
    assigned_costing_user = await _create_user(
        db_session,
        prefix="assigned-costing-review",
        role=UserRole.COSTING_TEAM,
        full_name="Assigned Costing Reviewer",
    )
    costing_user = await _create_user(
        db_session,
        prefix="unassigned-costing-review",
        role=UserRole.COSTING_TEAM,
        full_name="Unassigned Costing Reviewer",
    )
    _assign_matrix_contacts(monkeypatch, costing_email=assigned_costing_user.email)
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.FEASIBILITY,
        costing_file_state={"file_status": "PENDING"},
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/costing_review",
        json={"scope": True},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 403
    assert "costing agent" in response.json()["detail"]


@pytest.mark.asyncio
async def test_advance_to_pricing_requires_costing_file_action(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
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
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
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
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
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


@pytest.mark.asyncio
async def test_pricing_bom_upload_persists_file_in_costing_files(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="pricing-bom-creator",
        role=UserRole.COMMERCIAL,
        full_name="Pricing BOM Creator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="pricing-bom-user",
        role=UserRole.COSTING_TEAM,
        full_name="Pricing BOM User",
    )
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.PRICING,
        costing_file_state={"file_status": "UPLOADED"},
    )

    async def _fake_upload_costing_action_file(
        *, rfq_id, file, current_user_email, folder_name="costing"
    ):
        return {
            "id": f"{rfq_id}-pricing-bom",
            "filename": "pricing-bom.xlsx",
            "name": "pricing-bom.xlsx",
            "download_url": "https://example.com/pricing-bom.xlsx",
            "url": "https://example.com/pricing-bom.xlsx",
            "uploaded_by": current_user_email,
            "uploaded_at": "2026-04-16T12:00:00+00:00",
            "folder_name": folder_name,
        }

    monkeypatch.setattr(
        rfq_router,
        "_upload_costing_action_file",
        _fake_upload_costing_action_file,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/pricing-bom",
        data={"note": "BOM package ready for pricing."},
        files={"file": ("pricing-bom.xlsx", b"dummy-bytes", "application/vnd.ms-excel")},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 200
    payload = response.json()
    assert "pricing_bom_upload" not in (payload["rfq_data"] or {})
    assert payload["costing_files"][-1]["filename"] == "pricing-bom.xlsx"
    assert payload["costing_files"][-1]["file_role"] == "PRICING_BOM"
    assert payload["costing_files"][-1]["phase"] == "PRICING"
    assert payload["costing_files"][-1]["note"] == "BOM package ready for pricing."


@pytest.mark.asyncio
async def test_pricing_final_price_upload_requires_bom_and_persists_file(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    creator = await _create_user(
        db_session,
        prefix="pricing-final-creator",
        role=UserRole.COMMERCIAL,
        full_name="Pricing Creator",
    )
    costing_user = await _create_user(
        db_session,
        prefix="pricing-final-user",
        role=UserRole.COSTING_TEAM,
        full_name="Pricing User",
    )
    _assign_matrix_contacts(monkeypatch, costing_email=costing_user.email)
    rfq = await _create_rfq(
        db_session,
        creator=creator,
        phase=RfqPhase.COSTING,
        sub_status=RfqSubStatus.PRICING,
        costing_file_state={"file_status": "UPLOADED"},
    )

    rfq.costing_files = [
        {
            "id": f"{rfq.rfq_id}-bom",
            "filename": "pricing-bom.xlsx",
            "name": "pricing-bom.xlsx",
            "url": "https://example.com/pricing-bom.xlsx",
            "uploaded_by": costing_user.email,
            "uploaded_at": "2026-04-16T12:00:00+00:00",
            "file_role": "PRICING_BOM",
            "phase": "PRICING",
            "note": "BOM package uploaded",
        }
    ]
    await db_session.commit()

    async def _fake_upload_costing_action_file(
        *, rfq_id, file, current_user_email, folder_name="costing"
    ):
        return {
            "id": f"{rfq_id}-final-price",
            "filename": "final-price.xlsx",
            "name": "final-price.xlsx",
            "download_url": "https://example.com/final-price.xlsx",
            "url": "https://example.com/final-price.xlsx",
            "uploaded_by": current_user_email,
            "uploaded_at": "2026-04-16T13:00:00+00:00",
            "folder_name": folder_name,
        }

    monkeypatch.setattr(
        rfq_router,
        "_upload_costing_action_file",
        _fake_upload_costing_action_file,
    )

    response = await client.post(
        f"/api/rfq/{rfq.rfq_id}/pricing-final-price",
        data={"note": "Final customer price validated."},
        files={"file": ("final-price.xlsx", b"dummy-bytes", "application/vnd.ms-excel")},
        headers=_headers_for(costing_user),
    )

    assert response.status_code == 200
    payload = response.json()
    assert "pricing_final_price_upload" not in (payload["rfq_data"] or {})
    assert payload["costing_files"][-1]["filename"] == "final-price.xlsx"
    assert payload["costing_files"][-1]["file_role"] == "PRICING_FINAL_PRICE"
    assert payload["costing_files"][-1]["phase"] == "PRICING"
    assert payload["costing_files"][-1]["folder_name"] == "pricing-final-price"
    assert payload["costing_files"][-1]["note"] == "Final customer price validated."
