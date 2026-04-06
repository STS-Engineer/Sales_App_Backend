from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.contact import Contact
from app.models.potential import Potential
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.schemas.rfq import RfqOut
from app.models.user import User, UserRole
from app.services.potential import POTENTIAL_ALLOWED_FIELDS, update_potential_fields

router = APIRouter(prefix="/api/chat/potential", tags=["chat"])

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY or "dummy_key")

POTENTIAL_INITIAL_GREETING = (
    "Hello, I'm your potential opportunity assistant. "
    "I'll help you assess this opportunity before we open the formal RFQ.\n"
    "1. Guide me step by step\n"
    "2. I will provide a whole paragraph"
)

QUESTION_GUIDE: list[tuple[str, tuple[str, ...]]] = [
    (
        "Who is the customer and where are they located?",
        ("customer", "customer_location"),
    ),
    (
        "What is the application, and what industry is this product serving (Auto, Consumer, Industry...)?",
        ("application", "industry_served"),
    ),
    (
        "What kind of product do we plan to deliver (Assemblies, brushes, chokes, etc.)?",
        ("planned_product_type",),
    ),
    (
        "Why do we engage in such a project? Give reasons. Whose idea is this (Ours or The customer's)?",
        ("engagement_reasons", "idea_source"),
    ),
    (
        "Who is supplying the function today? (List competition)",
        ("current_supplier",),
    ),
    (
        "Why do we think we can take this business? (Competitiveness, Know-how, Proximity, Flexibility)",
        ("main_win_reason", "win_rationale_details"),
    ),
    (
        "Do we have technical capabilities? (Yes it is easy, Moderately complicated, Complex)",
        ("technical_capabilities",),
    ),
    (
        "Is it related to our strategy? (Integrates several components, Good margin, Mandatory to move forward)",
        ("strategic_fit", "strategic_fit_details"),
    ),
    (
        "What are the business perspectives? (Sales in k€, Margin in %, Start of production)",
        ("sales_keur", "margin_percentage", "start_of_production"),
    ),
    (
        "What are the development efforts to integrate (High, Medium, Low)?",
        ("development_effort",),
    ),
    (
        "What are the side effects of engaging (Gain more business, Acquire skills)?",
        ("side_effects",),
    ),
    (
        "What are the risks TO DO? (e.g., Lose IP, Spend money for nothing, Engage in price war)",
        ("risks_to_do",),
    ),
    (
        "What are the risks NOT TO DO? (e.g., Lose opportunity, Decrease current business)",
        ("risks_not_to_do",),
    ),
    (
        "Who is the contact name, email, phone number, and function?",
        ("contact_name", "contact_email", "contact_phone", "contact_function"),
    ),
]

POTENTIAL_TOOL_FIELD_PROPERTIES = {
    field_name: {"type": "number" if field_type == "float" else "string"}
    for field_name, field_type in POTENTIAL_ALLOWED_FIELDS.items()
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "checkGroupeExistence",
            "description": "Checks whether the customer already exists in the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                },
                "required": ["customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkContactExistence",
            "description": "Checks whether the contact already exists in the CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_email": {"type": "string"},
                },
                "required": ["contact_email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "updatePotentialFields",
            "description": "Saves the extracted Potential fields into the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fields_to_update": {
                        "type": "object",
                        "description": "Potential field values keyed by backend field name.",
                        "properties": POTENTIAL_TOOL_FIELD_PROPERTIES,
                    }
                },
                "required": ["fields_to_update"],
            },
        },
    },
]


class ChatRequest(BaseModel):
    rfq_id: str
    message: str


class ChatResponse(BaseModel):
    response: str
    tool_calls_used: list[str] | None = None
    rfq: RfqOut | None = None


def _can_view_rfq(current_user: User, rfq: Rfq) -> bool:
    return (
        current_user.role == UserRole.OWNER
        or rfq.created_by_email == current_user.email
        or rfq.zone_manager_email == current_user.email
    )


def _serialize_potential_state(potential: Potential) -> dict:
    return {
        "potential_systematic_id": potential.potential_systematic_id,
        "margin_keur": potential.margin_keur,
        **{
            field_name: getattr(potential, field_name)
            for field_name in POTENTIAL_ALLOWED_FIELDS.keys()
        },
    }


def _find_next_question(potential: Potential) -> tuple[str, list[str]]:
    for question, fields in QUESTION_GUIDE:
        missing = [
            field_name
            for field_name in fields
            if getattr(potential, field_name, None) in (None, "")
        ]
        if missing:
            return question, missing
    return (
        "All Potential fields are already captured. Summarize the opportunity and invite the user to proceed to the formal RFQ.",
        [],
    )


def _normalize_tool_calls(tool_calls) -> list[dict]:
    normalized_calls: list[dict] = []

    for index, tool_call in enumerate(tool_calls or [], start=1):
        if not hasattr(tool_call, "function"):
            continue

        func_name = tool_call.function.name
        raw_arguments = tool_call.function.arguments
        tool_call_id = tool_call.id or f"potential-tool-call-{index}"
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

        if func_name == "checkGroupeExistence" and "customer_name" not in parsed_arguments:
            parsed_arguments["customer_name"] = (
                parsed_arguments.get("groupeName")
                or parsed_arguments.get("customer")
                or ""
            )
        if func_name == "checkContactExistence" and "contact_email" not in parsed_arguments:
            parsed_arguments["contact_email"] = (
                parsed_arguments.get("email")
                or parsed_arguments.get("contactEmail")
                or ""
            )
        if func_name == "updatePotentialFields":
            fields = parsed_arguments.get("fields_to_update")
            if not isinstance(fields, dict):
                fields = {
                    key: value
                    for key, value in parsed_arguments.items()
                    if key != "fields_to_update"
                }
            parsed_arguments = {"fields_to_update": fields}

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


async def _execute_tool_calls(
    *,
    db: AsyncSession,
    potential: Potential,
    tool_calls: list[dict],
    tool_calls_used: list[str],
) -> list[dict]:
    tool_messages: list[dict] = []

    for tool_call in tool_calls:
        func_name = tool_call["name"]
        args = tool_call["arguments"]
        tool_calls_used.append(func_name)

        if func_name == "checkGroupeExistence":
            customer_name = str(args.get("customer_name") or "").strip()
            result = await db.execute(
                select(Contact).where(Contact.contact_name.ilike(f"%{customer_name}%"))
            )
            contacts = result.scalars().all()
            tool_response_text = json.dumps(
                {
                    "exists": bool(contacts),
                    "matches": [
                        {
                            "id": contact.contact_id,
                            "name": contact.contact_name,
                            "email": contact.contact_email,
                            "function": contact.contact_function,
                        }
                        for contact in contacts
                    ],
                }
            )
        elif func_name == "checkContactExistence":
            contact_email = str(args.get("contact_email") or "").strip()
            result = await db.execute(
                select(Contact).where(Contact.contact_email == contact_email)
            )
            contact = result.scalar_one_or_none()
            tool_response_text = json.dumps(
                {
                    "exists": bool(contact),
                    "contact": (
                        {
                            "id": contact.contact_id,
                            "email": contact.contact_email,
                            "name": contact.contact_name,
                            "first_name": contact.contact_first_name,
                            "function": contact.contact_function,
                            "phone": contact.contact_phone,
                        }
                        if contact
                        else None
                    ),
                }
            )
        elif func_name == "updatePotentialFields":
            fields = args.get("fields_to_update", {})
            filtered_fields, ignored_fields = await update_potential_fields(
                db=db,
                potential=potential,
                fields_to_update=fields,
            )
            await db.flush()
            tool_response_text = json.dumps(
                {
                    "success": True,
                    "fields_updated": list(filtered_fields.keys()),
                    "ignored_fields": ignored_fields,
                    "potential_systematic_id": potential.potential_systematic_id,
                    "margin_keur": potential.margin_keur,
                    "potential": _serialize_potential_state(potential),
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


@router.post("", response_model=ChatResponse)
async def handle_potential_chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Rfq)
        .options(selectinload(Rfq.potential))
        .where(Rfq.rfq_id == req.rfq_id)
    )
    rfq = result.scalar_one_or_none()
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    if not _can_view_rfq(current_user, rfq):
        raise HTTPException(status_code=403, detail="Not authorized to access this RFQ.")
    if (rfq.phase, rfq.sub_status) != (RfqPhase.RFQ, RfqSubStatus.POTENTIAL):
        raise HTTPException(
            status_code=409,
            detail="The Potential phase is locked for this RFQ.",
        )

    potential = rfq.potential
    if potential is None:
        potential = Potential(rfq_id=rfq.rfq_id, chat_history=[])
        db.add(potential)
        await db.flush()
        rfq.potential = potential

    history = list(potential.chat_history or [])
    if not history:
        history.append({"role": "assistant", "content": POTENTIAL_INITIAL_GREETING})
    history.append({"role": "user", "content": req.message})

    next_question, missing_fields = _find_next_question(potential)
    dynamic_prompt = f"""
You are the Potential Opportunity Intake Assistant. This is a pre-sales assessment before a formal RFQ exists.

You must ONLY use these database fields:
{json.dumps(sorted(POTENTIAL_ALLOWED_FIELDS.keys()), indent=2)}

Math rule:
- When the user provides Sales (k€) and Margin (%), you must calculate Margin (k€) autonomously and save it with updatePotentialFields.

Guided interview questions. Use these exact questions as your guide:
1. Who is the customer and where are they located?
2. What is the application, and what industry is this product serving (Auto, Consumer, Industry...)?
3. What kind of product do we plan to deliver (Assemblies, brushes, chokes, etc.)?
4. Why do we engage in such a project? Give reasons. Whose idea is this (Ours or The customer's)?
5. Who is supplying the function today? (List competition)
6. Why do we think we can take this business? (Competitiveness, Know-how, Proximity, Flexibility)
7. Do we have technical capabilities? (Yes it is easy, Moderately complicated, Complex)
8. Is it related to our strategy? (Integrates several components, Good margin, Mandatory to move forward)
9. What are the business perspectives? (Sales in k€, Margin in %, Start of production)
10. What are the development efforts to integrate (High, Medium, Low)?
11. What are the side effects of engaging (Gain more business, Acquire skills)?
12. What are the risks TO DO? (e.g., Lose IP, Spend money for nothing, Engage in price war)
13. What are the risks NOT TO DO? (e.g., Lose opportunity, Decrease current business)

Core rules:
- Save every valid user-provided field immediately with updatePotentialFields before you continue.
- Never ask for fields that are already populated in the current database state.
- Ask one focused question at a time unless the user chooses to provide a paragraph.
- If the user gives a full paragraph, extract every possible Potential field and save them in one updatePotentialFields call.
- Use checkGroupeExistence when the customer is provided.
- Use checkContactExistence when the contact email is provided.
- Keep responses concise and in Markdown.

Current next question:
{next_question}

Current missing fields for that question:
{json.dumps(missing_fields)}

Current Potential database state:
{json.dumps(_serialize_potential_state(potential), indent=2)}
""".strip()

    messages_for_llm = [{"role": "system", "content": dynamic_prompt}, *history[-20:]]

    tool_calls_used: list[str] = []
    final_text = ""

    try:
        completion = await client.chat.completions.create(
            model="gpt-5.2",
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
                potential=potential,
                tool_calls=normalized_tool_calls,
                tool_calls_used=tool_calls_used,
            )
            for tool_message in tool_messages:
                history.append(tool_message)
                messages_for_llm.append(tool_message)

            follow_up_completion = await client.chat.completions.create(
                model="gpt-5.2",
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
                "I've saved the latest Potential details. "
                "Please continue with the next missing information."
            )
        _append_assistant_text_if_new(history, final_text)
    except Exception as exc:
        final_text = (
            "**System error**\n\n"
            f"- The Potential assistant request failed.\n- Details: `{exc}`"
        )
        _append_assistant_text_if_new(history, final_text)

    potential.chat_history = history
    await db.commit()
    refreshed_result = await db.execute(
        select(Rfq)
        .options(selectinload(Rfq.potential))
        .where(Rfq.rfq_id == req.rfq_id)
    )
    refreshed_rfq = refreshed_result.scalar_one_or_none()

    return ChatResponse(
        response=final_text,
        tool_calls_used=tool_calls_used or None,
        rfq=refreshed_rfq,
    )
