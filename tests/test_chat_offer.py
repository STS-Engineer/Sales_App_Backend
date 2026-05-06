from __future__ import annotations

import pytest

from app.models.offer_preparation import OfferPreparation
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.routers.chat_offer import (
    _assign_offer_image_placement,
    _delete_offer_image_from_rfq,
    _execute_tool_calls,
    _handle_direct_offer_image_placement_request,
    _truncate_offer_chat_history_for_edit,
)
from app.services.offer_preparation_store import (
    get_offer_chat_history_snapshot,
    get_or_create_offer_preparation,
)


class _DummyDb:
    def add(self, _value) -> None:
        return None

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_execute_tool_calls_stores_offer_updates_separately_from_rfq_form_data():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "customer_name": "Base Customer",
            "expected_payment_terms": "30 days",
            "offer_chat_history": [],
        },
    )
    offer_preparation = OfferPreparation(rfq_id=rfq.rfq_id, offer_data={})
    tool_calls_used: list[str] = []

    await _execute_tool_calls(
        db=_DummyDb(),
        rfq=rfq,
        offer_preparation=offer_preparation,
        current_request_attachment_names=None,
        tool_calls=[
            {
                "id": "offer-tool-call-1",
                "name": "updateOfferFields",
                "arguments": {
                    "fields_to_update": {
                        "customer_name": "Offer Customer",
                        "copies": "Mohamed Laith Ben Mabrouk <mohamed@example.com>",
                        "pilot_quantity": "500 pcs",
                        "lead_time_deliveries": "6 weeks after order confirmation",
                        "expected_payment_terms": "60 days",
                        "inventory_commitment": "No inventory commitment requested",
                        "target_price_is_estimated": "yes",
                    }
                },
            }
        ],
        tool_calls_used=tool_calls_used,
    )

    assert tool_calls_used == ["updateOfferFields"]
    assert rfq.rfq_data["customer_name"] == "Base Customer"
    assert rfq.rfq_data["expected_payment_terms"] == "30 days"
    assert offer_preparation.offer_data["customer_name"] == "Offer Customer"
    assert (
        offer_preparation.offer_data["copies"]
        == "Mohamed Laith Ben Mabrouk <mohamed@example.com>"
    )
    assert offer_preparation.offer_data["pilot_quantity"] == "500 pcs"
    assert (
        offer_preparation.offer_data["lead_time_deliveries"]
        == "6 weeks after order confirmation"
    )
    assert offer_preparation.offer_data["expected_payment_terms"] == "60 days"
    assert (
        offer_preparation.offer_data["inventory_commitment"]
        == "No inventory commitment requested"
    )
    assert offer_preparation.offer_data["target_price_is_estimated"] is True


@pytest.mark.asyncio
async def test_get_or_create_offer_preparation_moves_legacy_offer_history_out_of_rfq_data():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-legacy-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "customer_name": "Base Customer",
            "offer_preparation_data": {"copies": "legacy-copy@example.com"},
            "offer_chat_history": [
                {"role": "assistant", "content": "Legacy offer discussion"}
            ],
        },
    )

    offer_preparation = await get_or_create_offer_preparation(_DummyDb(), rfq)

    assert offer_preparation.offer_data == {"copies": "legacy-copy@example.com"}
    assert offer_preparation.chat_history == [
        {"role": "assistant", "content": "Legacy offer discussion"}
    ]
    assert "offer_preparation_data" not in rfq.rfq_data
    assert "offer_chat_history" not in rfq.rfq_data


def test_get_offer_chat_history_snapshot_reads_only_offer_table_history():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-read-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "offer_chat_history": [
                {"role": "assistant", "content": "Legacy history should not be read"}
            ]
        },
    )

    assert get_offer_chat_history_snapshot(rfq) == []

    rfq.offer_preparation = OfferPreparation(
        rfq_id=rfq.rfq_id,
        chat_history=[{"role": "assistant", "content": "Persisted offer history"}],
    )

    assert get_offer_chat_history_snapshot(rfq) == [
        {"role": "assistant", "content": "Persisted offer history"}
    ]


def test_delete_offer_image_from_rfq_by_filename_removes_target_file_only():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-delete-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "rfq_files": [
                {
                    "id": "drawing-1",
                    "name": "drawing-preview.pdf",
                    "filename": "drawing-preview.pdf",
                    "content_type": "application/pdf",
                    "uploaded_at": "2026-05-05T12:00:00+00:00",
                },
                {
                    "id": "image-1",
                    "name": "before-product-picture-1.png",
                    "filename": "before-product-picture-1.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:10:00+00:00",
                },
            ]
        },
    )

    success, removed_name = _delete_offer_image_from_rfq(
        rfq,
        filename="before-product-picture-1.png",
    )

    assert success is True
    assert removed_name == "before-product-picture-1.png"
    remaining_names = [entry["name"] for entry in rfq.rfq_data["rfq_files"]]
    assert remaining_names == ["drawing-preview.pdf"]


def test_delete_offer_image_from_rfq_by_slot_and_index_removes_after_image():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-delete-002",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "rfq_files": [
                {
                    "id": "drawing-1",
                    "name": "drawing-preview.pdf",
                    "filename": "drawing-preview.pdf",
                    "content_type": "application/pdf",
                    "uploaded_at": "2026-05-05T12:00:00+00:00",
                },
                {
                    "id": "before-1",
                    "name": "before-product-picture-1.png",
                    "filename": "before-product-picture-1.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:10:00+00:00",
                },
                {
                    "id": "after-1",
                    "name": "after-product-picture-1.png",
                    "filename": "after-product-picture-1.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:20:00+00:00",
                },
            ]
        },
    )

    success, removed_name = _delete_offer_image_from_rfq(
        rfq,
        image_slot="reference_after",
        image_index=1,
    )

    assert success is True
    assert removed_name == "after-product-picture-1.png"
    remaining_names = [entry["name"] for entry in rfq.rfq_data["rfq_files"]]
    assert remaining_names == ["drawing-preview.pdf", "before-product-picture-1.png"]


def test_assign_offer_image_placement_uses_current_attachment_names_for_after_slot():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-place-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "rfq_files": [
                {
                    "id": "drawing-1",
                    "name": "drawing-preview.pdf",
                    "filename": "drawing-preview.pdf",
                    "content_type": "application/pdf",
                    "uploaded_at": "2026-05-05T12:00:00+00:00",
                },
                {
                    "id": "image-1",
                    "name": "new-product-picture.png",
                    "filename": "new-product-picture.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:10:00+00:00",
                },
            ]
        },
    )

    success, updated_names = _assign_offer_image_placement(
        rfq,
        image_slot="reference_after",
        fallback_attachment_names=["new-product-picture.png"],
    )

    assert success is True
    assert updated_names == ["new-product-picture.png"]
    image_entry = rfq.rfq_data["rfq_files"][1]
    assert image_entry["file_role"] == "REFERENCE_PICTURE_AFTER"


def test_assign_offer_image_placement_prefers_latest_file_when_names_repeat():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-place-duplicate-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "rfq_files": [
                {
                    "id": "drawing-1",
                    "name": "drawing-preview.pdf",
                    "filename": "drawing-preview.pdf",
                    "content_type": "application/pdf",
                    "uploaded_at": "2026-05-05T12:00:00+00:00",
                },
                {
                    "id": "image-older",
                    "name": "shared-picture.png",
                    "filename": "shared-picture.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:10:00+00:00",
                },
                {
                    "id": "image-newer",
                    "name": "shared-picture.png",
                    "filename": "shared-picture.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:20:00+00:00",
                },
            ]
        },
    )

    success, updated_names = _assign_offer_image_placement(
        rfq,
        image_slot="reference_after",
        fallback_attachment_names=["shared-picture.png"],
    )

    assert success is True
    assert updated_names == ["shared-picture.png"]
    assert rfq.rfq_data["rfq_files"][1].get("file_role", "") != "REFERENCE_PICTURE_AFTER"
    assert rfq.rfq_data["rfq_files"][2]["file_role"] == "REFERENCE_PICTURE_AFTER"


def test_direct_offer_image_placement_request_handles_after_slot_without_llm():
    rfq = Rfq(
        rfq_id="rfq-offer-chat-place-direct-001",
        phase=RfqPhase.OFFER,
        sub_status=RfqSubStatus.PREPARATION,
        created_by_email="seller@example.com",
        rfq_data={
            "rfq_files": [
                {
                    "id": "drawing-1",
                    "name": "drawing-preview.pdf",
                    "filename": "drawing-preview.pdf",
                    "content_type": "application/pdf",
                    "uploaded_at": "2026-05-05T12:00:00+00:00",
                },
                {
                    "id": "image-1",
                    "name": "new-product-picture.png",
                    "filename": "new-product-picture.png",
                    "content_type": "image/png",
                    "uploaded_at": "2026-05-05T12:10:00+00:00",
                },
            ]
        },
    )

    result = _handle_direct_offer_image_placement_request(
        rfq,
        message="add this picture after Product picture for reference",
        attachment_names=["new-product-picture.png"],
    )

    assert result is not None
    success, tool_calls_used, confirmation = result
    assert success is True
    assert tool_calls_used == ["assignOfferImagePlacement"]
    assert "after 'Product picture for reference'" in confirmation
    assert rfq.rfq_data["rfq_files"][1]["file_role"] == "REFERENCE_PICTURE_AFTER"


def test_truncate_offer_chat_history_for_edit_keeps_history_before_target_user_message():
    history = [
        {"role": "assistant", "content": "Hello, I'm your offer preparation assistant."},
        {"role": "user", "content": "first message"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "offer-tool-call-1",
                    "type": "function",
                    "function": {"name": "updateOfferFields", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "offer-tool-call-1",
            "name": "updateOfferFields",
            "content": '{"success": true}',
        },
        {"role": "assistant", "content": "First response"},
        {"role": "user", "content": "second message"},
        {"role": "assistant", "content": "Second response"},
    ]

    truncated_history = _truncate_offer_chat_history_for_edit(history, 3)

    assert truncated_history == history[:5]


def test_truncate_offer_chat_history_for_edit_rejects_non_user_visible_entries():
    history = [
        {"role": "assistant", "content": "Hello, I'm your offer preparation assistant."},
        {"role": "user", "content": "first message"},
    ]

    with pytest.raises(ValueError, match="Only user messages can be edited."):
        _truncate_offer_chat_history_for_edit(history, 0)
