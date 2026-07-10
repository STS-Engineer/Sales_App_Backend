import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_db3
from app.middleware.auth import get_current_user
from app.models.rfq import Rfq
from app.models.user import User
from app.models.validation_matrix import ValidationMatrix
from app.routers.chat import (
    TOOLS,
    client,
    _execute_tool_calls,
    _extract_tool_calls_from_text,
    _format_field_for_prompt,
    _get_required_missing_fields_before_submission,
    _normalize_rfq_data_fields,
    _normalize_tool_calls,
)
from app.routers.products import retrieve_products
from app.routers.rfq import _assert_can_edit_base_rfq_data
from app.services.routing import resolve_product_line_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat/autofill", tags=["chat-autofill"])

_AUTOFILL_TOOLS = [
    tool for tool in TOOLS if tool["function"]["name"] == "updateFormFields"
]

_AUTOFILL_SYSTEM_PROMPT_BASE = (
    "You are an RFQ assistant with two jobs:\n"
    "1. EXTRACTION MODE — when the user pastes a block of free text describing an RFQ "
    "opportunity (customer request, email, internal note, etc.), read the ENTIRE text and "
    "extract every RFQ field you can find, then call `updateFormFields` exactly once with "
    "everything you found in a single payload. Do not add any conversational text of your "
    "own in this case — the system will tell the user what is still missing after your "
    "tool call.\n"
    "2. Q&A MODE — when the user is instead asking a question (e.g. 'what fields are "
    "missing?', 'what did you fill in?', 'what's the customer name?', or any other "
    "question about this RFQ or how to use this tool), do NOT call `updateFormFields` — "
    "just answer the question directly and accurately in plain text, using the CURRENT "
    "RFQ STATE and MISSING REQUIRED FIELDS context given below as ground truth. Never "
    "make up information that isn't in that context.\n\n"
    "EXTRACTION RULES (apply only in Extraction mode):\n"
    "- Only create MULTIPLE rows in the `products` array when the text clearly describes "
    "multiple DISTINCT products being quoted separately (e.g. 'Product 1: ... Product 2: ...'). "
    "A single product's own sub-components, part numbers, drawings, or bill-of-materials items "
    "(e.g. a wire harness assembly listing its connector, grommet, terminals, and retainer as "
    "separate part numbers) are NOT separate products — they all belong to ONE product row. "
    "In that case, use the product's own name (e.g. 'Wire Harness') as `product`, and if several "
    "part numbers are listed for it, use the one that represents the complete/overall assembly "
    "as `part_number` — never create one row per part number or per sub-component.\n"
    "- If the text mentions multiple products, include all of them in the `products` array.\n"
    "- If a value is explicitly described as unknown/TBD/not provided, do not invent a "
    "value — simply omit that field.\n"
    "- Only fill `costing_data` with information the text EXPLICITLY presents as costing "
    "data (e.g. under a 'Costing Data' heading/label, or an explicit costing note). Never "
    "infer, summarize, or move other information (part numbers, drawings, scope details, "
    "etc.) into `costing_data` just because it seems useful — if the text doesn't "
    "explicitly give costing data, omit the field entirely.\n"
    "- For free-text fields that represent a LIST of separate items/requirements/statements "
    "in the source text (this applies especially to `business_trigger`, "
    "`customer_tooling_conditions`, `entry_barriers`, `technical_capacity`, "
    "`development_costs`, `type_of_packaging`, `scope`, `strategic_note`, and "
    "`final_recommendation`, but applies to any other field with the same list-like shape), "
    "put EACH distinct item/sentence on its OWN line, separated by '\\n' newlines — never "
    "join multiple items into one run-on sentence with commas or semicolons. Mirror how the "
    "source text separates them (bullets, line breaks, or separate sentences each become "
    "one line).\n"
    "- `expected_delivery_conditions` holds ONLY the Incoterm code (e.g. 'DDP', 'FCA', "
    "'EXW') — never merge Incoterm Location, payment terms, or any other logistics detail "
    "into it. The delivery location goes in `incoterm_location` instead; each field must "
    "hold only the single value it's meant for, never a combination of several fields' data."
)


def _build_missing_fields_summary(data: dict) -> str:
    missing_by_step = _get_required_missing_fields_before_submission(data)
    seen: set[str] = set()
    ordered_missing: list[str] = []
    for step_number in sorted(missing_by_step.keys()):
        for field_name in missing_by_step[step_number]:
            if field_name not in seen:
                seen.add(field_name)
                ordered_missing.append(field_name)
    if not ordered_missing:
        return "None — all required fields are currently filled in."
    return "\n".join(f"- {_format_field_for_prompt(name)}" for name in ordered_missing)


def _build_dynamic_system_prompt(data: dict) -> str:
    return (
        f"{_AUTOFILL_SYSTEM_PROMPT_BASE}\n\n"
        "=== CURRENT RFQ STATE ===\n"
        f"{json.dumps(data, indent=2, default=str)}\n\n"
        "=== MISSING REQUIRED FIELDS (Steps 1-3) ===\n"
        f"{_build_missing_fields_summary(data)}"
    )


class AutofillChatRequest(BaseModel):
    rfq_id: str
    message: str


class AutofillChatResponse(BaseModel):
    response: str


async def _resolve_product_line_acronym(db: AsyncSession, data: dict) -> str | None:
    """Derive the correct product_line_acronym from whatever product info was
    extracted, instead of trusting the model's own (often invalid) guess.

    Mirrors exactly what happens when a user manually fills the form: picking a
    product from the catalog (`GET /api/products`, the same source behind the
    "Product" dropdown) auto-fills its acronym. We look up each extracted
    product name in that same catalog first; only if nothing matches there do
    we fall back to treating the extracted text as a product-line name/acronym
    itself (exact, then fuzzy). Returns None if the product genuinely isn't in
    the catalog — the user then picks the product line manually, same as today.
    """
    product_names: list[str] = []
    products = data.get("products")
    if isinstance(products, list):
        for product in products:
            if isinstance(product, dict) and product.get("product"):
                product_names.append(str(product["product"]))
    product_name = data.get("product_name")
    if product_name:
        product_names.append(str(product_name))

    for candidate in product_names:
        normalized = candidate.strip()
        if not normalized:
            continue
        try:
            catalog = await retrieve_products(productName=normalized, db=db)
        except Exception:
            continue
        for item in catalog.products:
            item_name = str(item.get("product_name") or "").strip()
            if item_name.lower() == normalized.lower() and item.get("acronym"):
                return item["acronym"]

    candidates: list[str] = list(product_names)
    top_level_acronym = data.get("product_line_acronym")
    if top_level_acronym:
        candidates.append(str(top_level_acronym))

    for candidate in candidates:
        context = await resolve_product_line_context(db, identifier=candidate)
        if context:
            return context["acronym"]

    normalized_candidates = [c.strip().lower() for c in candidates if c.strip()]
    if not normalized_candidates:
        return None

    result = await db.execute(select(ValidationMatrix))
    for row in result.scalars().all():
        product_line_lower = row.product_line.lower()
        # Crude singular forms (e.g. "brushes" -> "brush", "chokes" -> "choke") to
        # catch the common plural/singular mismatch between the catalog name and
        # free text — English pluralization isn't uniform (-s vs -es), so try
        # both a 1- and 2-character trim.
        singular_forms = {product_line_lower}
        if product_line_lower.endswith("s") and len(product_line_lower) > 4:
            singular_forms.add(product_line_lower[:-1])
            singular_forms.add(product_line_lower[:-2])
        for candidate in normalized_candidates:
            if product_line_lower in candidate or candidate in product_line_lower:
                return row.acronym
            if any(len(form) >= 4 and form in candidate for form in singular_forms):
                return row.acronym

    return None


_PLACEHOLDER_VALUES = {
    "tbd", "tba", "tbc", "n/a", "na", "unknown", "not specified", "not provided",
    "not applicable", "to be confirmed", "to be determined", "to be advised",
    "not given", "none provided", "not stated",
}


def _strip_placeholder_values(data: dict) -> None:
    """The model sometimes writes the literal placeholder text (e.g. "TBD") into a
    field instead of omitting it as instructed — which then wrongly counts as
    "filled" for missing-field purposes. Scrub known placeholders back to empty
    everywhere (top-level fields and each product row), rather than relying only
    on the model to follow the "omit unknown values" instruction.
    """
    def _clean(value):
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDER_VALUES:
            return ""
        return value

    for key, value in list(data.items()):
        if key in ("products", "volumes"):
            continue
        data[key] = _clean(value)

    products = data.get("products")
    if isinstance(products, list):
        for product in products:
            if isinstance(product, dict):
                for key, value in list(product.items()):
                    product[key] = _clean(value)


async def _persist_autofill_turn(
    db: AsyncSession,
    rfq: Rfq,
    existing_history: list,
    message: str,
    response_text: str,
) -> None:
    """Append this exchange to the RFQ's shared chat_history (the same column the
    main docked assistant reads/writes) so the autofill bubble's conversation
    survives a page refresh instead of living only in local React state.
    """
    rfq.chat_history = [
        *existing_history,
        {"role": "user", "content": message},
        {"role": "assistant", "content": response_text},
    ]
    await db.commit()


def _build_completion_message(data: dict) -> str:
    missing_by_step = _get_required_missing_fields_before_submission(data)
    has_missing_fields = any(missing_by_step.values())

    if not has_missing_fields:
        return "Done! I've filled in everything I could find. You can review the form and submit when ready."

    return (
        "Done! I've filled in everything I could find from your text. Please check "
        "the form and manually complete any remaining required fields."
    )


@router.post("", response_model=AutofillChatResponse)
async def autofill_chat(
    req: AutofillChatRequest,
    db: AsyncSession = Depends(get_db),
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Rfq).where(Rfq.rfq_id == req.rfq_id))
    rfq = result.scalar_one_or_none()
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    _assert_can_edit_base_rfq_data(current_user, rfq)

    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required.")

    existing_history = list(rfq.chat_history or [])
    extracted_data = _normalize_rfq_data_fields(rfq.rfq_data)

    messages_for_llm = [
        {"role": "system", "content": _build_dynamic_system_prompt(extracted_data)},
        {"role": "user", "content": message},
    ]

    try:
        completion = await client.chat.completions.create(
            model="gpt-5.2",
            messages=messages_for_llm,
            tools=_AUTOFILL_TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
    except Exception:
        logger.exception("Autofill chat completion failed for rfq_id=%s", req.rfq_id)
        raise HTTPException(
            status_code=502, detail="The assistant is unavailable right now."
        )

    ai_message = completion.choices[0].message
    normalized_tool_calls = _normalize_tool_calls(ai_message.tool_calls)
    if not normalized_tool_calls:
        normalized_tool_calls = _extract_tool_calls_from_text(
            ai_message.content or "", {"updateFormFields"}
        )

    if not normalized_tool_calls:
        # Q&A mode: the model chose to answer directly instead of extracting —
        # trust its plain-text reply (built from the injected RFQ state/missing
        # fields context) rather than forcing it through the extraction pipeline.
        answer = (ai_message.content or "").strip() or (
            "I couldn't find any RFQ details in that text. Paste the customer, "
            "product, or technical information you'd like me to extract and fill "
            "into the form."
        )
        await _persist_autofill_turn(db, rfq, existing_history, message, answer)
        return AutofillChatResponse(response=answer)

    fields_were_updated = False
    try:
        if normalized_tool_calls:
            tool_calls_used: list[str] = []
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as http_client:
                tool_messages, _redirect = await _execute_tool_calls(
                    tool_calls=normalized_tool_calls,
                    http_client=http_client,
                    db=db,
                    db3=db3,
                    rfq=rfq,
                    current_user=current_user,
                    extracted_data=extracted_data,
                    chat_mode="rfq",
                    tool_calls_used=tool_calls_used,
                )

            for tool_message in tool_messages:
                try:
                    payload = json.loads(tool_message.get("content") or "{}")
                except (TypeError, ValueError):
                    continue
                if payload.get("fields_updated"):
                    fields_were_updated = True
                    break

            if fields_were_updated:
                _strip_placeholder_values(extracted_data)
                resolved_acronym = await _resolve_product_line_acronym(db, extracted_data)
                extracted_data["product_line_acronym"] = resolved_acronym or ""
                rfq.product_line_acronym = resolved_acronym
                # Each product row also carries its own `product_line` field (shown by
                # the frontend whenever the top-level product_line_acronym is empty) —
                # it must be kept in sync, otherwise a hallucinated value can resurface
                # there even after the top-level field is corrected/cleared.
                products = extracted_data.get("products")
                if isinstance(products, list):
                    for product in products:
                        if isinstance(product, dict):
                            product["product_line"] = resolved_acronym or ""
                rfq.rfq_data = extracted_data

        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Autofill chat persistence failed for rfq_id=%s", req.rfq_id)
        response_text = (
            "I extracted most of the fields, but couldn't save one of them "
            "automatically. Please review the form — some fields may need to be "
            "entered manually."
        )
        await _persist_autofill_turn(db, rfq, existing_history, message, response_text)
        return AutofillChatResponse(response=response_text)

    if not fields_were_updated:
        response_text = (
            "I couldn't find any RFQ details in that text. Paste the customer, "
            "product, or technical information you'd like me to extract and fill "
            "into the form."
        )
    else:
        response_text = _build_completion_message(rfq.rfq_data or {})

    await _persist_autofill_turn(db, rfq, existing_history, message, response_text)
    return AutofillChatResponse(response=response_text)
