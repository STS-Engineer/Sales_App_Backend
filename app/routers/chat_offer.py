from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from openai import APITimeoutError, AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.offer_preparation import OfferPreparation
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.models.user import User
from app.routers.rfq import (
    _assert_can_edit_base_rfq_data,
    _delete_azure_blob,
    _delete_legacy_local_file,
)
from app.schemas.rfq import RfqOut
from app.services.offer_preparation_store import (
    get_offer_chat_history_snapshot,
    get_offer_preparation_data_snapshot,
    get_or_create_offer_preparation,
)
from app.services.offer_template import (
    DOCX_IMAGE_EXTENSIONS,
    DRAWING_FILE_ROLE_HINTS,
    DRAWING_NAME_HINTS,
    REFERENCE_PICTURE_AFTER_NAME_HINTS,
    REFERENCE_PICTURE_AFTER_ROLE_HINTS,
    REFERENCE_PICTURE_BEFORE_NAME_HINTS,
    REFERENCE_PICTURE_BEFORE_ROLE_HINTS,
    build_offer_preparation_context,
    _select_annex_drawing_file,
    _select_reference_picture_groups,
)

router = APIRouter(prefix="/api/chat/offer", tags=["chat"])

OPENAI_TIMEOUT_SECONDS = 180.0
MODEL_NAME = "gpt-5.2"

client = AsyncOpenAI(
    api_key=settings.OPENAI_API_KEY or "dummy_key",
    http_client=httpx.AsyncClient(timeout=httpx.Timeout(OPENAI_TIMEOUT_SECONDS)),
)

OFFER_INITIAL_GREETING = (
    "Hello, I'm your offer preparation assistant. I can help you review the "
    "fields used in the offer Word template. Tell me what you want to update, "
    "or ask me to check what is still missing."
)

OFFER_FIELD_ORDER: tuple[str, ...] = (
    "customer_name",
    "product_name",
    "project_name",
    "customer_pn",
    "revision_level",
    "your_reference",
    "copies",
    "subject",
    "contact_name",
    "contact_phone",
    "contact_email",
    "sop_year",
    "validation_batch",
    "annual_volume",
    "pilot_quantity",
    "pilot_unit_price",
    "material_balance_moq",
    "serial_unit_price",
    "expected_delivery_conditions",
    "lead_time_deliveries",
    "expected_payment_terms",
    "inventory_commitment",
    "offer_validity",
    "type_of_packaging",
    "target_price_eur",
    "target_price_local",
    "target_price_currency",
    "target_price_is_estimated",
)

OFFER_FIELD_LABELS = {
    "customer_name": "customer name",
    "product_name": "product name",
    "project_name": "project name",
    "customer_pn": "customer part number",
    "revision_level": "revision level",
    "your_reference": "your reference",
    "copies": "copies / CC recipients",
    "subject": "offer subject",
    "contact_name": "contact name",
    "contact_phone": "contact phone",
    "contact_email": "contact email",
    "sop_year": "SOP year",
    "validation_batch": "validation batch",
    "annual_volume": "annual volume",
    "pilot_quantity": "pilot quantity",
    "pilot_unit_price": "pilot unit price",
    "material_balance_moq": "material balance MOQ",
    "serial_unit_price": "serial unit price",
    "expected_delivery_conditions": "delivery conditions",
    "lead_time_deliveries": "lead time and deliveries",
    "expected_payment_terms": "payment terms",
    "inventory_commitment": "inventory commitment",
    "offer_validity": "offer validity",
    "type_of_packaging": "packaging type",
    "target_price_eur": "target price in EUR",
    "target_price_local": "target price in local currency",
    "target_price_currency": "target price currency",
    "target_price_is_estimated": "target price estimated flag",
}

OFFER_TOOL_FIELD_PROPERTIES = {
    field_name: {"type": "boolean" if field_name == "target_price_is_estimated" else "string"}
    for field_name in OFFER_FIELD_ORDER
}
IMAGE_SLOT_TO_FILE_ROLE = {
    "annex_drawing": "ANNEX_DRAWING",
    "reference_before": "REFERENCE_PICTURE_BEFORE",
    "reference_after": "REFERENCE_PICTURE_AFTER",
}
OFFER_IMAGE_PLACEMENT_LABELS = {
    "annex_drawing": "as the annex drawing",
    "reference_before": "before 'Product picture for reference'",
    "reference_after": "after 'Product picture for reference'",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "updateOfferFields",
            "description": (
                "Saves offer-preparation field values into the dedicated offer "
                "template data for the current RFQ. Use only the supported backend keys."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fields_to_update": {
                        "type": "object",
                        "properties": OFFER_TOOL_FIELD_PROPERTIES,
                    }
                },
                "required": ["fields_to_update"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deleteOfferImage",
            "description": (
                "Deletes an RFQ image/file used by the Offer Word template. "
                "Use filename when the user names the file. Use image_slot and image_index "
                "when the user refers to annex drawing, the images before 'Product picture for reference', "
                "or the images after it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "image_slot": {
                        "type": "string",
                        "enum": ["annex_drawing", "reference_before", "reference_after"],
                    },
                    "image_index": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assignOfferImagePlacement",
            "description": (
                "Assigns one or more RFQ image files to the correct location in the Offer Word template. "
                "Use this whenever the user asks to add or move an image before or after 'Product picture for reference', "
                "or to use a file as the annex drawing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "filenames": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "image_slot": {
                        "type": "string",
                        "enum": ["annex_drawing", "reference_before", "reference_after"],
                    },
                },
                "required": ["image_slot"],
            },
        },
    },
]


class ChatRequest(BaseModel):
    rfq_id: str
    message: str
    attachment_names: list[str] | None = None


class ChatEditRequest(BaseModel):
    rfq_id: str
    visible_message_index: int
    message: str


class ChatResponse(BaseModel):
    response: str
    tool_calls_used: list[str] | None = None
    rfq: RfqOut | None = None


def _is_field_filled(value) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value is not None


def _stringify_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value).strip()


def _serialize_offer_state(
    rfq: Rfq,
    rfq_data: dict,
    offer_data: dict,
) -> dict:
    effective_data = _build_offer_chat_field_values(rfq, rfq_data, offer_data)
    return {
        field_name: effective_data.get(field_name)
        for field_name in OFFER_FIELD_ORDER
        if _is_field_filled(effective_data.get(field_name))
    } | {
        "phase": rfq.phase.value,
        "sub_status": rfq.sub_status.value,
    }


def _guess_content_type(value: str | None) -> str:
    guessed_type = mimetypes.guess_type(str(value or ""))[0]
    return str(guessed_type or "").lower()


def _stringify_filename(value: Any) -> str:
    return str(value or "").strip()


def _get_normalized_offer_rfq_files_with_source(rfq_data: dict) -> list[dict]:
    raw_files = rfq_data.get("rfq_files")
    if not isinstance(raw_files, list):
        return []

    normalized_files: list[dict] = []
    for source_index, entry in enumerate(raw_files):
        if isinstance(entry, str):
            normalized_files.append(
                {
                    "source_index": source_index,
                    "name": _stringify_filename(entry).split("/")[-1],
                    "filename": _stringify_filename(entry).split("/")[-1],
                    "uploaded_at": "",
                    "path": _stringify_filename(entry),
                    "url": _stringify_filename(entry),
                    "download_url": _stringify_filename(entry),
                    "blob_url": "",
                    "blob_name": "",
                    "id": "",
                    "file_role": "",
                    "content_type": _guess_content_type(entry),
                }
            )
            continue
        if not isinstance(entry, dict):
            continue

        normalized_files.append(
            {
                "source_index": source_index,
                "name": _stringify_filename(
                    entry.get("name")
                    or entry.get("filename")
                    or entry.get("original_name")
                    or entry.get("file_name")
                ),
                "filename": _stringify_filename(
                    entry.get("filename")
                    or entry.get("name")
                    or entry.get("original_name")
                    or entry.get("file_name")
                ),
                "uploaded_at": _stringify_filename(
                    entry.get("uploaded_at")
                    or entry.get("updated_at")
                    or entry.get("last_modified")
                ),
                "path": _stringify_filename(entry.get("path")),
                "url": _stringify_filename(entry.get("url")),
                "download_url": _stringify_filename(entry.get("download_url")),
                "blob_url": _stringify_filename(entry.get("blob_url")),
                "blob_name": _stringify_filename(entry.get("blob_name")),
                "id": _stringify_filename(entry.get("id") or entry.get("file_id")),
                "file_role": _stringify_filename(entry.get("file_role")),
                "content_type": _stringify_filename(entry.get("content_type")),
            }
        )

    return [entry for entry in normalized_files if entry.get("name")]


def _build_offer_image_inventory(rfq_data: dict) -> dict[str, list[dict]]:
    normalized_files = _get_normalized_offer_rfq_files_with_source(rfq_data)
    annex_file = _select_annex_drawing_file(normalized_files)
    reference_picture_groups = _select_reference_picture_groups(normalized_files, annex_file)

    inventory = {
        "annex_drawing": [annex_file] if annex_file else [],
        "reference_before": list(reference_picture_groups.get("before") or []),
        "reference_after": list(reference_picture_groups.get("after") or []),
    }
    return inventory


def _format_offer_image_inventory_for_prompt(rfq_data: dict) -> dict[str, list[str]]:
    inventory = _build_offer_image_inventory(rfq_data)
    return {
        slot: [
            f"{index + 1}. {entry.get('name')}"
            for index, entry in enumerate(entries)
            if _stringify_filename(entry.get("name"))
        ]
        for slot, entries in inventory.items()
    }


def _parse_sortable_timestamp(value: str | None) -> float:
    normalized_value = _stringify_filename(value)
    if not normalized_value:
        return 0.0
    try:
        return datetime.fromisoformat(normalized_value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _find_latest_offer_file_match(
    normalized_files: list[dict],
    desired_name: str,
) -> dict | None:
    normalized_name = _stringify_filename(desired_name).casefold()
    matching_entries = [
        entry
        for entry in normalized_files
        if _stringify_filename(entry.get("filename") or entry.get("name")).casefold()
        == normalized_name
    ]
    if not matching_entries:
        return None

    matching_entries.sort(
        key=lambda entry: (
            _parse_sortable_timestamp(entry.get("uploaded_at")),
            int(entry.get("source_index") or 0),
        ),
        reverse=True,
    )
    return matching_entries[0]


def _resolve_offer_image_target(
    rfq_data: dict,
    *,
    filename: str = "",
    image_slot: str = "",
    image_index: int | None = None,
) -> tuple[dict | None, str]:
    normalized_filename = _stringify_filename(filename).casefold()
    inventory = _build_offer_image_inventory(rfq_data)
    all_entries = [entry for entries in inventory.values() for entry in entries]

    if normalized_filename:
        matched_entry = next(
            (
                entry
                for entry in all_entries
                if _stringify_filename(entry.get("filename") or entry.get("name")).casefold()
                == normalized_filename
            ),
            None,
        )
        if matched_entry is None:
            return None, "No matching image was found for that filename."

        matched_slot = next(
            (
                slot
                for slot, entries in inventory.items()
                if any(entry is matched_entry for entry in entries)
            ),
            "",
        )
        return matched_entry, matched_slot

    normalized_slot = _stringify_filename(image_slot)
    if normalized_slot:
        entries = list(inventory.get(normalized_slot) or [])
        if not entries:
            return None, "There is no image in that offer section."

        if image_index is None:
            if len(entries) == 1:
                return entries[0], normalized_slot
            return None, "Please specify which image index to delete in that section."

        if image_index < 1 or image_index > len(entries):
            return None, "The requested image index does not exist in that section."

        return entries[image_index - 1], normalized_slot

    if len(all_entries) == 1:
        only_entry = all_entries[0]
        only_slot = next(
            (
                slot
                for slot, entries in inventory.items()
                if any(entry is only_entry for entry in entries)
            ),
            "",
        )
        return only_entry, only_slot

    if not all_entries:
        return None, "There is no offer image to delete."

    return None, "Please specify the image filename or whether it is annex, before, or after the product picture heading."


def _delete_offer_image_from_rfq(
    rfq: Rfq,
    *,
    filename: str = "",
    image_slot: str = "",
    image_index: int | None = None,
) -> tuple[bool, str]:
    extracted_data = dict(rfq.rfq_data or {})
    raw_files = extracted_data.get("rfq_files")
    existing_files = list(raw_files) if isinstance(raw_files, list) else []

    target_entry, error_message = _resolve_offer_image_target(
        extracted_data,
        filename=filename,
        image_slot=image_slot,
        image_index=image_index,
    )
    if target_entry is None:
        return False, error_message

    source_index = target_entry.get("source_index")
    if not isinstance(source_index, int) or source_index < 0 or source_index >= len(existing_files):
        return False, "The target image could not be resolved in the RFQ file list."

    removed_file = existing_files.pop(source_index)
    if isinstance(removed_file, dict):
        _delete_azure_blob(removed_file)
        _delete_legacy_local_file(removed_file)

    extracted_data["rfq_files"] = existing_files
    if existing_files:
        latest_file = existing_files[-1]
        if isinstance(latest_file, dict):
            extracted_data["rfq_file_path"] = (
                latest_file.get("url")
                or latest_file.get("download_url")
                or latest_file.get("path")
            )
        else:
            extracted_data["rfq_file_path"] = str(latest_file)
    else:
        extracted_data.pop("rfq_file_path", None)

    rfq.rfq_data = extracted_data
    return True, _stringify_filename(
        (removed_file or {}).get("filename") if isinstance(removed_file, dict) else removed_file
    ) or _stringify_filename(
        (removed_file or {}).get("name") if isinstance(removed_file, dict) else removed_file
    )


def _assign_offer_image_placement(
    rfq: Rfq,
    *,
    image_slot: str,
    filename: str = "",
    filenames: list[str] | None = None,
    fallback_attachment_names: list[str] | None = None,
) -> tuple[bool, list[str] | str]:
    normalized_slot = _stringify_value(image_slot)
    target_role = IMAGE_SLOT_TO_FILE_ROLE.get(normalized_slot)
    if not target_role:
        return False, "Unsupported image slot."

    extracted_data = dict(rfq.rfq_data or {})
    raw_files = extracted_data.get("rfq_files")
    existing_files = list(raw_files) if isinstance(raw_files, list) else []
    normalized_files = _get_normalized_offer_rfq_files_with_source(extracted_data)

    desired_names = [
        _stringify_value(value)
        for value in [filename, *(filenames or []), *(fallback_attachment_names or [])]
        if _stringify_value(value)
    ]
    deduped_names: list[str] = []
    seen_names: set[str] = set()
    for value in desired_names:
        key = value.casefold()
        if key in seen_names:
            continue
        seen_names.add(key)
        deduped_names.append(value)

    target_entries: list[dict] = []
    if deduped_names:
        for desired_name in deduped_names:
            matched_entry = _find_latest_offer_file_match(normalized_files, desired_name)
            if matched_entry is None:
                return False, f"No uploaded image matches '{desired_name}'."
            target_entries.append(matched_entry)
    else:
        candidate_entries = [
            entry
            for entry in normalized_files
            if _is_image_candidate_for_offer_slot(entry, normalized_slot)
        ]
        if not candidate_entries:
            return False, "No matching uploaded image is available for that offer section."
        target_entries.append(candidate_entries[0])

    updated_names: list[str] = []
    for target_entry in target_entries:
        source_index = target_entry.get("source_index")
        if not isinstance(source_index, int) or source_index < 0 or source_index >= len(existing_files):
            continue
        raw_entry = existing_files[source_index]
        if not isinstance(raw_entry, dict):
            continue
        raw_entry["file_role"] = target_role
        updated_names.append(
            _stringify_filename(raw_entry.get("filename") or raw_entry.get("name"))
        )

    if not updated_names:
        return False, "The selected image could not be updated."

    extracted_data["rfq_files"] = existing_files
    rfq.rfq_data = extracted_data
    return True, updated_names


def _is_image_candidate_for_offer_slot(file_entry: dict, image_slot: str) -> bool:
    content_type = _stringify_value(file_entry.get("content_type")).lower()
    file_name = _stringify_filename(file_entry.get("name")).lower()
    file_role = _stringify_value(file_entry.get("file_role")).upper()
    is_image = content_type.startswith("image/") or any(
        file_name.endswith(extension) for extension in DOCX_IMAGE_EXTENSIONS
    )
    if image_slot == "annex_drawing":
        return (
            content_type == "application/pdf"
            or file_name.endswith(".pdf")
            or file_role in DRAWING_FILE_ROLE_HINTS
            or any(hint in file_name for hint in DRAWING_NAME_HINTS)
        )
    if image_slot == "reference_before":
        return is_image
    if image_slot == "reference_after":
        return is_image
    return False


def _detect_offer_image_slot_from_message(message: str) -> str:
    normalized_message = re.sub(r"\s+", " ", _stringify_value(message)).casefold()
    if not normalized_message:
        return ""

    if "product picture for reference" in normalized_message:
        if any(token in normalized_message for token in (" after ", " below ", " under ")):
            return "reference_after"
        if any(token in normalized_message for token in (" before ", " above ")):
            return "reference_before"

    if any(token in normalized_message for token in ("annex drawing", "annex 1", "annex1")):
        return "annex_drawing"

    if "product picture" in normalized_message or "reference picture" in normalized_message:
        if any(token in normalized_message for token in (" after ", " below ", " under ")):
            return "reference_after"
        if any(token in normalized_message for token in (" before ", " above ")):
            return "reference_before"

    return ""


def _build_offer_image_placement_confirmation(
    image_slot: str,
    updated_names: list[str],
) -> str:
    placement_label = OFFER_IMAGE_PLACEMENT_LABELS.get(image_slot, "in the offer template")
    if len(updated_names) == 1:
        return f"I placed `{updated_names[0]}` {placement_label}."
    return (
        f"I placed {len(updated_names)} images {placement_label}: "
        + ", ".join(f"`{name}`" for name in updated_names)
        + "."
    )


def _handle_direct_offer_image_placement_request(
    rfq: Rfq,
    *,
    message: str,
    attachment_names: list[str] | None,
) -> tuple[bool, list[str], str] | None:
    normalized_attachment_names = [
        _stringify_value(value)
        for value in list(attachment_names or [])
        if _stringify_value(value)
    ]
    if not normalized_attachment_names:
        return None

    image_slot = _detect_offer_image_slot_from_message(message)
    if not image_slot:
        return None

    success, result = _assign_offer_image_placement(
        rfq,
        image_slot=image_slot,
        fallback_attachment_names=normalized_attachment_names,
    )
    if not success:
        return False, ["assignOfferImagePlacement"], _stringify_value(result)

    updated_names = list(result) if isinstance(result, list) else [_stringify_value(result)]
    return (
        True,
        ["assignOfferImagePlacement"],
        _build_offer_image_placement_confirmation(image_slot, updated_names),
    )


def _get_missing_offer_fields(rfq: Rfq, rfq_data: dict, offer_data: dict) -> list[str]:
    effective_data = _build_offer_chat_field_values(rfq, rfq_data, offer_data)
    return [
        field_name
        for field_name in OFFER_FIELD_ORDER
        if not _is_field_filled(effective_data.get(field_name))
    ]


def _build_offer_chat_field_values(rfq: Rfq, rfq_data: dict, offer_data: dict) -> dict:
    context = build_offer_preparation_context(rfq, offer_data=offer_data)
    effective_data = dict(rfq_data or {})
    effective_data.update(offer_data or {})
    field_values = {
        field_name: effective_data.get(field_name) for field_name in OFFER_FIELD_ORDER
    }

    for field_name in OFFER_FIELD_ORDER:
        context_value = context.get(field_name)
        if _is_field_filled(context_value):
            field_values[field_name] = context_value

    return field_values


def _normalize_tool_calls(tool_calls) -> list[dict]:
    normalized_calls: list[dict] = []

    for index, tool_call in enumerate(tool_calls or [], start=1):
        if not hasattr(tool_call, "function"):
            continue

        func_name = tool_call.function.name
        raw_arguments = tool_call.function.arguments
        tool_call_id = tool_call.id or f"offer-tool-call-{index}"
        if not func_name:
            continue

        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                parsed_arguments = {}
        elif isinstance(raw_arguments, dict):
            parsed_arguments = raw_arguments
        else:
            parsed_arguments = {}

        if func_name == "updateOfferFields":
            fields = parsed_arguments.get("fields_to_update")
            if not isinstance(fields, dict):
                fields = {
                    key: value
                    for key, value in parsed_arguments.items()
                    if key != "fields_to_update"
                }
            parsed_arguments = {"fields_to_update": fields}
        elif func_name == "deleteOfferImage":
            parsed_arguments = {
                "filename": _stringify_value(parsed_arguments.get("filename")),
                "image_slot": _stringify_value(parsed_arguments.get("image_slot")),
                "image_index": parsed_arguments.get("image_index"),
            }
        elif func_name == "assignOfferImagePlacement":
            raw_filenames = parsed_arguments.get("filenames")
            parsed_arguments = {
                "filename": _stringify_value(parsed_arguments.get("filename")),
                "filenames": [
                    _stringify_value(value)
                    for value in list(raw_filenames or [])
                    if _stringify_value(value)
                ],
                "image_slot": _stringify_value(parsed_arguments.get("image_slot")),
            }

        normalized_calls.append(
            {
                "id": tool_call_id,
                "name": func_name,
                "arguments": parsed_arguments,
            }
        )

    return normalized_calls


def _build_tool_call_assistant_message(tool_calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": json.dumps(tool_call["arguments"]),
                },
            }
            for tool_call in tool_calls
        ],
    }


def _sanitize_assistant_text(content: str | None) -> str:
    return str(content or "").strip()


def _append_assistant_text_if_new(history: list[dict], content: str) -> str:
    text = _sanitize_assistant_text(content)
    if not text:
        return ""
    if history:
        last_message = history[-1]
        if (
            last_message.get("role") == "assistant"
            and str(last_message.get("content") or "").strip() == text
        ):
            return text
    history.append({"role": "assistant", "content": text})
    return text


def _map_visible_offer_chat_entries(history: list[dict] | None) -> list[dict]:
    visible_entries: list[dict] = []

    for raw_index, entry in enumerate(list(history or [])):
        if not isinstance(entry, dict):
            continue

        role = entry.get("role")
        content = entry.get("content")
        if role not in {"assistant", "user"}:
            continue
        if role == "assistant" and entry.get("tool_calls"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue

        visible_entries.append(
            {
                "raw_index": raw_index,
                "role": role,
                "content": content,
            }
        )

    return visible_entries


def _truncate_offer_chat_history_for_edit(
    history: list[dict] | None,
    visible_message_index: int,
) -> list[dict]:
    if visible_message_index < 0:
        raise ValueError("Invalid chat message index.")

    raw_history = [dict(entry) for entry in list(history or []) if isinstance(entry, dict)]
    visible_history = _map_visible_offer_chat_entries(raw_history)

    if visible_message_index >= len(visible_history):
        raise LookupError("Chat message not found.")

    target_entry = visible_history[visible_message_index]
    if target_entry["role"] != "user":
        raise ValueError("Only user messages can be edited.")

    return list(raw_history[: target_entry["raw_index"]])


async def _execute_tool_calls(
    *,
    db: AsyncSession,
    rfq: Rfq,
    offer_preparation: OfferPreparation,
    current_request_attachment_names: list[str] | None,
    tool_calls: list[dict],
    tool_calls_used: list[str],
) -> list[dict]:
    tool_messages: list[dict] = []

    for tool_call in tool_calls:
        func_name = tool_call["name"]
        args = tool_call["arguments"]
        tool_calls_used.append(func_name)

        if func_name == "updateOfferFields":
            raw_fields = args.get("fields_to_update", {})
            fields = dict(raw_fields) if isinstance(raw_fields, dict) else {}
            filtered_fields = {
                key: value
                for key, value in fields.items()
                if key in OFFER_FIELD_ORDER
            }

            if "target_price_is_estimated" in filtered_fields:
                value = filtered_fields["target_price_is_estimated"]
                filtered_fields["target_price_is_estimated"] = (
                    value
                    if isinstance(value, bool)
                    else str(value).strip().lower() in {"true", "1", "yes"}
                )

            offer_preparation_data = dict(offer_preparation.offer_data or {})
            for key, value in filtered_fields.items():
                if key == "target_price_is_estimated":
                    offer_preparation_data[key] = bool(value)
                else:
                    offer_preparation_data[key] = _stringify_value(value)

            offer_preparation.offer_data = offer_preparation_data
            await db.flush()

            tool_response_text = json.dumps(
                {
                    "success": True,
                    "fields_updated": list(filtered_fields.keys()),
                    "ignored_fields": sorted(
                        set(fields.keys()) - set(filtered_fields.keys())
                    ),
                }
            )
        elif func_name == "deleteOfferImage":
            success, result_message = _delete_offer_image_from_rfq(
                rfq,
                filename=_stringify_value(args.get("filename")),
                image_slot=_stringify_value(args.get("image_slot")),
                image_index=args.get("image_index"),
            )
            if success:
                await db.flush()
                tool_response_text = json.dumps(
                    {
                        "success": True,
                        "deleted_image": result_message,
                    }
                )
            else:
                tool_response_text = json.dumps(
                    {
                        "success": False,
                        "error": result_message,
                    }
                )
        elif func_name == "assignOfferImagePlacement":
            success, result_message = _assign_offer_image_placement(
                rfq,
                image_slot=_stringify_value(args.get("image_slot")),
                filename=_stringify_value(args.get("filename")),
                filenames=[
                    _stringify_value(value)
                    for value in list(args.get("filenames") or [])
                    if _stringify_value(value)
                ],
                fallback_attachment_names=current_request_attachment_names,
            )
            if success:
                await db.flush()
                tool_response_text = json.dumps(
                    {
                        "success": True,
                        "updated_images": result_message,
                        "image_slot": _stringify_value(args.get("image_slot")),
                    }
                )
            else:
                tool_response_text = json.dumps(
                    {
                        "success": False,
                        "error": result_message,
                    }
                )
        else:
            tool_response_text = json.dumps(
                {"success": False, "error": f"Unsupported tool: {func_name}"}
            )

        tool_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": func_name,
                "content": tool_response_text,
            }
        )

    return tool_messages


def _build_system_prompt(
    rfq: Rfq,
    rfq_data: dict,
    offer_data: dict,
    current_request_attachment_names: list[str] | None = None,
) -> str:
    missing_fields = _get_missing_offer_fields(rfq, rfq_data, offer_data)
    missing_labels = [OFFER_FIELD_LABELS.get(field, field) for field in missing_fields]
    current_state = _serialize_offer_state(rfq, rfq_data, offer_data)
    image_inventory = _format_offer_image_inventory_for_prompt(rfq_data)
    attachment_names = [
        _stringify_value(value)
        for value in list(current_request_attachment_names or [])
        if _stringify_value(value)
    ]

    return f"""
You are the Offer Preparation Assistant for an existing RFQ.

Core mission:
- Help the user review and complete the fields used to fill the existing Word offer template.
- Focus only on the Offer Preparation phase.
- Do not restart the RFQ workflow.
- Do not discuss validation routing, costing workflow, or unrelated tabs unless the user explicitly asks.

Strict tool rule:
- Every time the user provides or corrects one of the supported fields, you MUST immediately call `updateOfferFields`.
- Every time the user asks to delete/remove an image used in the offer document, you MUST immediately call `deleteOfferImage`.
- Every time the user asks to add or move an uploaded image before or after 'Product picture for reference', or to use it as the annex drawing, you MUST immediately call `assignOfferImagePlacement`.
- Never print raw JSON or tool payloads in the visible response.
- Never ask again for a field that is already present in the current state unless the user wants to change it.

Supported backend keys:
{json.dumps({field: OFFER_FIELD_LABELS[field] for field in OFFER_FIELD_ORDER}, indent=2)}

Current missing fields:
{json.dumps(missing_labels, indent=2)}

Current RFQ database state:
{json.dumps(current_state, indent=2)}

Current offer image inventory:
{json.dumps(image_inventory, indent=2)}

Current request attachment names:
{json.dumps(attachment_names, indent=2)}

Response style:
- Keep replies concise and practical.
- Ask only one focused follow-up question at a time when information is missing.
- If all current template fields are already filled, briefly ask what the user wants to revise or confirm.
- If the user says "this picture" or "this image" and the current request has attachment names, use those attachments in `assignOfferImagePlacement`.
""".strip()


@router.post("/edit", response_model=ChatResponse)
async def edit_offer_chat_message(
    req: ChatEditRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Rfq)
        .options(selectinload(Rfq.offer_preparation))
        .where(Rfq.rfq_id == req.rfq_id)
    )
    rfq = result.scalar_one_or_none()

    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    _assert_can_edit_base_rfq_data(current_user, rfq)

    edited_message = str(req.message or "").strip()
    if not edited_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    offer_preparation = await get_or_create_offer_preparation(db, rfq)
    history = get_offer_chat_history_snapshot(rfq, offer_preparation)

    try:
        truncated_history = _truncate_offer_chat_history_for_edit(
            history,
            req.visible_message_index,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    offer_preparation.chat_history = truncated_history
    await db.flush()

    return await handle_offer_chat(
        ChatRequest(rfq_id=req.rfq_id, message=edited_message),
        db=db,
        current_user=current_user,
    )


@router.post("", response_model=ChatResponse)
async def handle_offer_chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Rfq)
        .options(selectinload(Rfq.offer_preparation))
        .where(Rfq.rfq_id == req.rfq_id)
    )
    rfq = result.scalar_one_or_none()

    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    _assert_can_edit_base_rfq_data(current_user, rfq)

    if rfq.phase != RfqPhase.OFFER:
        raise HTTPException(
            status_code=409,
            detail="The offer assistant is only available during the Offer phase.",
        )

    if rfq.sub_status != RfqSubStatus.PREPARATION:
        raise HTTPException(
            status_code=409,
            detail="The offer assistant is only editable during Offer preparation.",
        )

    rfq_data = dict(rfq.rfq_data or {})
    offer_preparation = await get_or_create_offer_preparation(db, rfq)
    offer_data = get_offer_preparation_data_snapshot(rfq, offer_preparation)
    history = get_offer_chat_history_snapshot(rfq, offer_preparation)

    if not history:
        history.append({"role": "assistant", "content": OFFER_INITIAL_GREETING})
    history.append({"role": "user", "content": req.message})

    direct_image_placement_result = _handle_direct_offer_image_placement_request(
        rfq,
        message=req.message,
        attachment_names=req.attachment_names,
    )
    if direct_image_placement_result is not None:
        success, direct_tool_calls_used, direct_response = direct_image_placement_result
        tool_calls_used = list(direct_tool_calls_used)
        final_text = direct_response or (
            "The offer image placement was updated. Tell me what you want to change next."
        )
        _append_assistant_text_if_new(history, final_text)
        offer_preparation.chat_history = history
        await db.commit()

        refreshed_result = await db.execute(
            select(Rfq)
            .options(selectinload(Rfq.offer_preparation))
            .where(Rfq.rfq_id == req.rfq_id)
        )
        refreshed_rfq = refreshed_result.scalar_one_or_none()
        return ChatResponse(
            response=final_text,
            tool_calls_used=tool_calls_used or None,
            rfq=refreshed_rfq,
        )

    messages_for_llm = [
        {
            "role": "system",
            "content": _build_system_prompt(
                rfq,
                rfq_data,
                offer_data,
                current_request_attachment_names=req.attachment_names,
            ),
        },
        *history[-20:],
    ]

    tool_calls_used: list[str] = []
    final_text = ""

    try:
        completion = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages_for_llm,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
        ai_message = completion.choices[0].message
        normalized_tool_calls = _normalize_tool_calls(getattr(ai_message, "tool_calls", None))

        if normalized_tool_calls:
            assistant_tool_message = _build_tool_call_assistant_message(normalized_tool_calls)
            history.append(assistant_tool_message)
            messages_for_llm.append(assistant_tool_message)

            tool_messages = await _execute_tool_calls(
                db=db,
                rfq=rfq,
                offer_preparation=offer_preparation,
                current_request_attachment_names=req.attachment_names,
                tool_calls=normalized_tool_calls,
                tool_calls_used=tool_calls_used,
            )
            for tool_message in tool_messages:
                history.append(tool_message)
                messages_for_llm.append(tool_message)

            rfq_data = dict(rfq.rfq_data or {})
            offer_data = get_offer_preparation_data_snapshot(rfq, offer_preparation)
            messages_for_llm[0] = {
                "role": "system",
                "content": _build_system_prompt(
                    rfq,
                    rfq_data,
                    offer_data,
                    current_request_attachment_names=req.attachment_names,
                ),
            }

            follow_up_completion = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages_for_llm,
                temperature=0.2,
            )
            final_text = _sanitize_assistant_text(
                follow_up_completion.choices[0].message.content
            )
        else:
            final_text = _sanitize_assistant_text(ai_message.content)

        if not final_text:
            final_text = (
                "The offer data was updated. Tell me what you want to review next."
            )
        _append_assistant_text_if_new(history, final_text)
    except (httpx.TimeoutException, APITimeoutError):
        final_text = (
            "**System error**\n\n"
            "- The offer assistant took too long to respond.\n"
            "- Please try again in a moment."
        )
        _append_assistant_text_if_new(history, final_text)
    except Exception as exc:
        final_text = (
            "**System error**\n\n"
            f"- The offer assistant request failed.\n- Details: `{exc}`"
        )
        _append_assistant_text_if_new(history, final_text)

    offer_preparation.chat_history = history
    await db.commit()

    refreshed_result = await db.execute(
        select(Rfq)
        .options(selectinload(Rfq.offer_preparation))
        .where(Rfq.rfq_id == req.rfq_id)
    )
    refreshed_rfq = refreshed_result.scalar_one_or_none()

    return ChatResponse(
        response=final_text,
        tool_calls_used=tool_calls_used or None,
        rfq=refreshed_rfq,
    )
