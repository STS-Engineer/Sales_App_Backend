import json
import datetime
import re
import unicodedata
import httpx
from openai import AsyncOpenAI
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.routers.rfq import submit_rfq_for_validation
from app.models.user import User
from app.models.validation_matrix import ValidationMatrix

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Async OpenAI client
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY or "dummy_key")

BASE_URL = "https://rfq-api.azurewebsites.net"
INITIAL_GREETING = (
    "Please select your preferred language.\n"
    "1- English\n"
    "2- Français\n"
    "3- 中文\n"
    "4- Español\n"
    "5- Deutsch\n"
    "6- हिन्दी"
)
LANGUAGE_SELECTION_RULE = (
    "CRITICAL RULE: The user will respond to the initial greeting with a language "
    "choice (either by number or name). You MUST instantly acknowledge their choice "
    "in that specific language. From that point forward, conduct the ENTIRE "
    "conversation, including all step-by-step guidance and missing field "
    "announcements, strictly in their chosen language."
)
LANGUAGE_OPTIONS: dict[str, dict[str, object]] = {
    "en": {
        "menu_number": "1",
        "name": "English",
        "aliases": {"1", "english", "en"},
        "assistant_intro": (
            "Hello, I'm your sales assistant. I'll be helping you fill your RFQ. "
            "Do you want me to guide you step-by-step, or you can give me a whole "
            "paragraph and I'll extract the needed fields for each step and tell "
            "you the missing fields?"
        ),
    },
    "fr": {
        "menu_number": "2",
        "name": "Français",
        "aliases": {"2", "français", "francais", "french", "fr"},
        "assistant_intro": (
            "Bonjour, je suis votre assistant commercial. Je vais vous aider à "
            "remplir votre RFQ. Souhaitez-vous que je vous guide étape par étape, "
            "ou préférez-vous me donner un paragraphe complet afin que j'extraie "
            "les champs nécessaires pour chaque étape et que je vous indique les "
            "informations manquantes ?"
        ),
    },
    "zh": {
        "menu_number": "3",
        "name": "中文",
        "aliases": {"3", "中文", "chinese", "mandarin", "zh"},
        "assistant_intro": (
            "您好，我是您的销售助手。我将帮助您填写 RFQ。您希望我一步一步引导您，"
            "还是您可以直接给我一整段文字，我会为每个步骤提取所需字段并告诉您"
            "还缺少哪些信息？"
        ),
    },
    "es": {
        "menu_number": "4",
        "name": "Español",
        "aliases": {"4", "español", "espanol", "spanish", "es"},
        "assistant_intro": (
            "Hola, soy su asistente comercial. Le ayudaré a completar su RFQ. "
            "¿Quiere que le guíe paso a paso, o puede darme un párrafo completo y "
            "yo extraeré los campos necesarios para cada etapa y le diré qué "
            "información falta?"
        ),
    },
    "de": {
        "menu_number": "5",
        "name": "Deutsch",
        "aliases": {"5", "deutsch", "german", "de"},
        "assistant_intro": (
            "Hallo, ich bin Ihr Vertriebsassistent. Ich helfe Ihnen beim Ausfüllen "
            "Ihrer RFQ. Möchten Sie, dass ich Sie Schritt für Schritt führe, oder "
            "können Sie mir einen ganzen Absatz geben und ich extrahiere die "
            "benötigten Felder für jeden Schritt und teile Ihnen mit, welche "
            "Informationen noch fehlen?"
        ),
    },
    "hi": {
        "menu_number": "6",
        "name": "हिन्दी",
        "aliases": {"6", "हिन्दी", "हिंदी", "hindi", "hi"},
        "assistant_intro": (
            "नमस्ते, मैं आपका सेल्स असिस्टेंट हूँ। मैं आपका RFQ भरने में मदद करूँगा। "
            "क्या आप चाहते हैं कि मैं आपको चरण-दर-चरण मार्गदर्शन दूँ, या आप मुझे "
            "एक पूरा पैराग्राफ दे सकते हैं और मैं हर चरण के लिए आवश्यक फ़ील्ड निकालकर "
            "बताऊँगा कि कौन-सी जानकारी अभी बाकी है?"
        ),
    },
}
POTENTIAL_ALLOWED_FIELDS = {
    "customer_name",
    "application",
    "contact_email",
    "contact_first_name",
    "contact_name",
    "contact_role",
    "contact_phone",
}
RFQ_ALLOWED_FIELDS = POTENTIAL_ALLOWED_FIELDS | {
    "product_name",
    "product_line_acronym",
    "costing_data",
    "customer_pn",
    "revision_level",
    "delivery_zone",
    "delivery_plant",
    "country",
    "sop_year",
    "annual_volume",
    "rfq_reception_date",
    "quotation_expected_date",
    "target_price_eur",
    "expected_delivery_conditions",
    "expected_payment_terms",
    "business_trigger",
    "customer_tooling_conditions",
    "entry_barriers",
    "responsibility_design",
    "responsibility_validation",
    "product_ownership",
    "pays_for_development",
    "capacity_available",
    "scope",
    "customer_status",
    "strategic_note",
    "final_recommendation",
    "to_total",
    "zone_manager_email",
}
POTENTIAL_STEPS: list[tuple[int, list[str]]] = [
    (1, ["customer_name", "application"]),
    (2, ["contact_email"]),
    (3, ["contact_first_name", "contact_name", "contact_role", "contact_phone"]),
]
RFQ_STEPS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            "customer_name",
            "application",
            "product_name",
            "product_line_acronym",
            "rfq_files",
            "customer_pn",
            "revision_level",
            "delivery_zone",
            "delivery_plant",
            "country",
            "sop_year",
            "annual_volume",
            "rfq_reception_date",
            "quotation_expected_date",
            "contact_email",
            "contact_first_name",
            "contact_name",
            "contact_role",
            "contact_phone",
        ],
    ),
    (
        2,
        [
            "target_price_eur",
            "expected_delivery_conditions",
            "expected_payment_terms",
            "business_trigger",
            "customer_tooling_conditions",
            "entry_barriers",
        ],
    ),
    (
        3,
        [
            "responsibility_design",
            "responsibility_validation",
            "product_ownership",
            "pays_for_development",
            "capacity_available",
            "scope",
        ],
    ),
    (4, ["to_total", "zone_manager_email"]),
]


def _normalize_scope_value(value):
    if isinstance(value, bool):
        return "In scope" if value else "Out of scope"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return "In scope"
        if normalized == "false":
            return "Out of scope"
    return value


def _normalize_rfq_data_fields(data: dict | None) -> dict:
    normalized = dict(data or {})
    legacy_scope = normalized.pop("is_feasible", None)
    if "scope" not in normalized and legacy_scope is not None:
        normalized["scope"] = _normalize_scope_value(legacy_scope)
    return normalized


def _normalize_language_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _detect_language_choice(message: str | None) -> dict[str, object] | None:
    raw_message = str(message or "").strip()
    if not raw_message:
        return None

    numeric_match = re.fullmatch(r"([1-6])(?:\s*[-.)]?\s*[^\d].*)?", raw_message)
    if numeric_match:
        selected_number = numeric_match.group(1)
        for code, option in LANGUAGE_OPTIONS.items():
            if option["menu_number"] == selected_number:
                return {"code": code, **option}

    normalized_message = _normalize_language_token(raw_message)
    for code, option in LANGUAGE_OPTIONS.items():
        aliases = {
            _normalize_language_token(alias)
            for alias in option["aliases"]  # type: ignore[index]
        }
        if normalized_message in aliases:
            return {"code": code, **option}
        if any(alias and alias in normalized_message for alias in aliases):
            return {"code": code, **option}

    return None


def _build_language_context(preferred_language_code: str | None) -> str:
    if not preferred_language_code:
        return (
            "No preferred language has been stored yet. Treat the current user "
            "message as their response to the language-selection menu. Do not start "
            "the RFQ workflow until you have either acknowledged one of the 6 "
            "supported languages or asked the user to choose again from the menu."
        )

    preferred = LANGUAGE_OPTIONS.get(preferred_language_code)
    if not preferred:
        return (
            "A preferred language code is stored, but it is not recognized. Ask the "
            "user to pick one of the 6 supported languages from the menu."
        )

    return (
        f"The preferred language is {preferred['name']}. You MUST write every "
        f"response strictly in {preferred['name']}. Keep all future guidance, "
        f"questions, confirmations, and status updates in that language."
    )


def _normalize_chat_mode(rfq: Rfq, requested_mode: str) -> str:
    mode = (requested_mode or "rfq").strip().lower()
    if mode == "potential":
        return "potential"
    if rfq.phase == RfqPhase.RFQ and rfq.sub_status == RfqSubStatus.POTENTIAL:
        return "potential"
    return "rfq"


def _is_field_filled(data: dict, field_name: str) -> bool:
    if field_name == "rfq_files":
        value = (
            data.get("rfq_files")
            or data.get("files")
            or data.get("attachments")
            or data.get("rfq_file_path")
            or data.get("rfq_file_paths")
        )
    else:
        value = data.get(field_name)

    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return value is not None


def _get_current_step_and_missing_fields(chat_mode: str, data: dict) -> tuple[int, list[str]]:
    steps = POTENTIAL_STEPS if chat_mode == "potential" else RFQ_STEPS
    for step_number, fields in steps:
        missing_fields = [field for field in fields if not _is_field_filled(data, field)]
        if missing_fields:
            return step_number, missing_fields
    return steps[-1][0], []


def _build_missing_fields_prompt(chat_mode: str, data: dict) -> str:
    current_step, missing_fields = _get_current_step_and_missing_fields(chat_mode, data)
    prompt = (
        f"The user is currently on Step {current_step}. "
        f"The following required fields are empty: {missing_fields}. Ask the user for these."
    )
    if not missing_fields:
        prompt += (
            " All required fields for this step are already present, so move to the next "
            "workflow action instead of re-asking completed fields."
        )
    return prompt


def _filter_update_fields(chat_mode: str, fields: dict) -> dict:
    allowed_fields = POTENTIAL_ALLOWED_FIELDS if chat_mode == "potential" else RFQ_ALLOWED_FIELDS
    return {key: value for key, value in fields.items() if key in allowed_fields}


def _is_crash_message(content: str) -> bool:
    text = str(content or "")
    return "System encountered an error" in text or "system encountered an error" in text.lower()


def _strip_fenced_payload(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return text


def _try_load_json_payload(content: str):
    text = _strip_fenced_payload(content)
    if not text:
        return None

    candidates = [text]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if 0 <= first_brace < last_brace:
        candidates.append(text[first_brace : last_brace + 1])

    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if 0 <= first_bracket < last_bracket:
        candidates.append(text[first_bracket : last_bracket + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _normalize_tool_arguments(func_name: str, args: dict | None) -> dict:
    normalized = dict(args or {})

    if func_name == "checkGroupeExistence":
        normalized["groupeName"] = (
            normalized.get("groupeName")
            or normalized.get("customerName")
            or normalized.get("customer_name")
            or normalized.get("groupName")
            or normalized.get("groupe")
        )
    elif func_name == "retrieveProducts":
        normalized["productName"] = (
            normalized.get("productName")
            or normalized.get("product_name")
            or ""
        )
    elif func_name == "checkContactExistence":
        normalized["contact_email"] = (
            normalized.get("contact_email")
            or normalized.get("contactEmail")
            or normalized.get("email")
        )
    elif func_name == "retrieveZoneManager":
        normalized["to_total"] = (
            normalized.get("to_total")
            or normalized.get("toTotal")
            or normalized.get("to_total_keur")
        )
        normalized["product_line_acronym"] = (
            normalized.get("product_line_acronym")
            or normalized.get("productLineAcronym")
            or normalized.get("productLine")
            or normalized.get("product_line")
        )
    elif func_name == "updateFormFields":
        fields = normalized.get("fields_to_update")
        if not isinstance(fields, dict):
            fields = {
                key: value
                for key, value in normalized.items()
                if key != "fields_to_update"
            }
        legacy_scope = fields.pop("is_feasible", None) if isinstance(fields, dict) else None
        if isinstance(fields, dict) and legacy_scope is not None and "scope" not in fields:
            fields["scope"] = _normalize_scope_value(legacy_scope)
        normalized = {"fields_to_update": fields if isinstance(fields, dict) else {}}
    elif func_name == "uploadRfqFiles" and "file_confirmed" not in normalized:
        normalized["file_confirmed"] = bool(
            normalized.get("confirmed", normalized.get("fileConfirmed", True))
        )

    return normalized


def _normalize_tool_calls(tool_calls) -> list[dict]:
    normalized_calls: list[dict] = []

    for index, tool_call in enumerate(tool_calls or [], start=1):
        if hasattr(tool_call, "function"):
            func_name = tool_call.function.name
            raw_arguments = tool_call.function.arguments
            tool_call_id = tool_call.id or f"tool-call-{index}"
        else:
            func_name = tool_call.get("name")
            raw_arguments = tool_call.get("arguments", {})
            tool_call_id = tool_call.get("id") or f"tool-call-{index}"

        if not func_name:
            continue

        if isinstance(raw_arguments, str):
            parsed_arguments = _try_load_json_payload(raw_arguments)
            raw_arguments = parsed_arguments if isinstance(parsed_arguments, dict) else {}
        elif not isinstance(raw_arguments, dict):
            raw_arguments = {}

        normalized_calls.append(
            {
                "id": tool_call_id,
                "name": func_name,
                "arguments": _normalize_tool_arguments(func_name, raw_arguments),
            }
        )

    return normalized_calls


def _extract_tool_calls_from_text(content: str) -> list[dict]:
    parsed_payload = _try_load_json_payload(content)
    if parsed_payload is None:
        return []

    if isinstance(parsed_payload, dict) and "tool_calls" in parsed_payload:
        raw_calls = parsed_payload.get("tool_calls") or []
    elif isinstance(parsed_payload, dict):
        raw_calls = [parsed_payload]
    elif isinstance(parsed_payload, list):
        raw_calls = parsed_payload
    else:
        return []

    normalized_calls: list[dict] = []
    for index, item in enumerate(raw_calls, start=1):
        if not isinstance(item, dict):
            continue

        func_name = item.get("toolname") or item.get("tool_name") or item.get("name")
        if not func_name and isinstance(item.get("function"), dict):
            func_name = item["function"].get("name")

        if func_name not in ALLOWED_TOOL_NAMES:
            continue

        raw_arguments = (
            item.get("arguments")
            or item.get("args")
            or item.get("parameters")
        )
        if raw_arguments is None and isinstance(item.get("function"), dict):
            raw_arguments = item["function"].get("arguments", {})

        if isinstance(raw_arguments, str):
            parsed_arguments = _try_load_json_payload(raw_arguments)
            raw_arguments = parsed_arguments if isinstance(parsed_arguments, dict) else {}
        elif not isinstance(raw_arguments, dict):
            raw_arguments = {}

        normalized_calls.append(
            {
                "id": item.get("toolcallid")
                or item.get("tool_call_id")
                or item.get("id")
                or f"pseudo-tool-call-{index}",
                "name": func_name,
                "arguments": _normalize_tool_arguments(func_name, raw_arguments),
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


async def _execute_tool_calls(
    *,
    tool_calls: list[dict],
    http_client: httpx.AsyncClient,
    db: AsyncSession,
    rfq: Rfq,
    current_user: User,
    extracted_data: dict,
    chat_mode: str,
    tool_calls_used: list[str],
) -> list[dict]:
    tool_messages: list[dict] = []

    for tool_call in tool_calls:
        func_name = tool_call["name"]
        args = tool_call["arguments"]
        tool_calls_used.append(func_name)

        tool_response_text = ""

        if func_name == "checkGroupeExistence":
            groupe = args.get("groupeName")
            if groupe:
                extracted_data["customer_name"] = groupe
            resp = await http_client.get(
                f"{BASE_URL}/api/data/groupe/check",
                params={"groupeName": groupe},
            )
            tool_response_text = resp.text

        elif func_name == "retrieveProducts":
            prod_name = args.get("productName", "")
            resp = await http_client.get(
                f"{BASE_URL}/api/products",
                params={"productName": prod_name},
            )
            tool_response_text = resp.text

        elif func_name == "uploadRfqFiles":
            tool_response_text = json.dumps(
                {"files_uploaded": True, "db_status": "synced"}
            )

        elif func_name == "checkContactExistence":
            email = args.get("contact_email")
            if email:
                extracted_data["contact_email"] = email
            resp = await http_client.get(
                f"{BASE_URL}/api/contact/check",
                params={"email": email},
            )
            tool_response_text = resp.text

        elif func_name == "retrieveZoneManager":
            to_total_val = args.get("to_total")
            acronym = args.get("product_line_acronym")

            try:
                to_total_float = float(to_total_val)
                query = select(ValidationMatrix).where(
                    ValidationMatrix.acronym == acronym
                )
                result = await db.execute(query)
                matrix = result.scalar_one_or_none()

                if matrix:
                    if to_total_float <= matrix.n3_kam_limit:
                        required_role = "KAM"
                    elif to_total_float <= matrix.n2_zone_limit:
                        required_role = "Zone Manager"
                    elif to_total_float <= matrix.n1_vp_limit:
                        required_role = "VP Sales"
                    else:
                        required_role = "CEO"

                    zone_manager_email = None
                    delivery_zone = extracted_data.get("delivery_zone", "").lower()

                    if required_role == "KAM":
                        zone_manager_email = rfq.created_by_email
                    elif required_role == "VP Sales":
                        zone_manager_email = "eric.suszylo@avocarbon.com"
                    elif required_role == "CEO":
                        zone_manager_email = "olivier.spicker@avocarbon.com"
                    elif required_role == "Zone Manager":
                        if "asie est" in delivery_zone or "east asia" in delivery_zone:
                            zone_manager_email = "tao.ren@avocarbon.com"
                        elif "asie sud" in delivery_zone or "south asia" in delivery_zone:
                            zone_manager_email = "eipe.thomas@avocarbon.com"
                        elif "europe" in delivery_zone:
                            zone_manager_email = "franck.lagadec@avocarbon.com"
                        elif (
                            "amÃ©rique" in delivery_zone
                            or "america" in delivery_zone
                            or "amerique" in delivery_zone
                        ):
                            zone_manager_email = "dean.hayward@avocarbon.com"
                        else:
                            zone_manager_email = "franck.lagadec@avocarbon.com"

                    tool_response_text = json.dumps(
                        {
                            "role_assigned": required_role,
                            "zone_manager_email": zone_manager_email,
                        }
                    )
                else:
                    tool_response_text = json.dumps(
                        {
                            "error": (
                                f"Product line '{acronym}' not found in validation matrix."
                            )
                        }
                    )
            except Exception as e:
                tool_response_text = json.dumps({"error": str(e)})

        elif func_name == "submitValidation":
            try:
                submit_result = await submit_rfq_for_validation(
                    rfq_id=rfq.rfq_id,
                    db=db,
                    current_user=current_user,
                )
                await db.refresh(rfq)
                extracted_data.clear()
                extracted_data.update(_normalize_rfq_data_fields(rfq.rfq_data))
                tool_response_text = json.dumps(
                    {
                        "success": True,
                        "message": submit_result.get(
                            "message",
                            "RFQ submitted for validation.",
                        ),
                        "phase": rfq.phase.value,
                        "sub_status": rfq.sub_status.value,
                        "zone_manager_email": rfq.zone_manager_email,
                    }
                )
            except HTTPException as exc:
                detail = exc.detail
                if not isinstance(detail, str):
                    detail = json.dumps(detail)
                tool_response_text = json.dumps(
                    {
                        "success": False,
                        "error": detail,
                        "status_code": exc.status_code,
                    }
                )
            except Exception as exc:
                tool_response_text = json.dumps(
                    {"success": False, "error": str(exc)}
                )

        elif func_name == "updateFormFields":
            fields = args.get("fields_to_update", {})
            filtered_fields = _filter_update_fields(chat_mode, fields)
            for key, value in filtered_fields.items():
                extracted_data[key] = str(value)
            tool_response_text = json.dumps(
                {
                    "success": True,
                    "fields_updated": list(filtered_fields.keys()),
                    "ignored_fields": sorted(
                        set(fields.keys()) - set(filtered_fields.keys())
                    ),
                    "status": "extracted_to_form",
                }
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

class ChatRequest(BaseModel):
    rfq_id: str
    message: str
    chat_mode: str = "rfq"

class ChatResponse(BaseModel):
    response: str
    tool_calls_used: list[str] | None = None

SYSTEM_PROMPT = LANGUAGE_SELECTION_RULE + """

STRICT ANTI-HALLUCINATION DIRECTIVE:
Under NO CIRCUMSTANCES are you allowed to guess, assume, or fabricate the existence of a Customer, Product, Product Line, or Contact.
If the user provides a Customer name, you MUST call the checkGroupeExistence tool. YOU MUST WAIT for the system to return the JSON response containing the database result.
DO NOT generate a text response confirming or denying the customer until you have physically received the tool_call_id response from the system. If you violate this rule, the system will fail.

*** UNIVERSAL DATA SAVING RULE ***: EVERY SINGLE TIME the user provides a piece of information (e.g., Application, chosen Product Name, Contact Info, Target Price, Quantities, Dates, etc.), you MUST immediately call the 'updateFormFields' tool to save that specific data point to the database. You can call 'updateFormFields' at the exact same time as you ask your next question. If you fail to call 'updateFormFields', the UI will break.

STRICT FORM FIELD MAPPING:
When calling updateFormFields, you MUST ONLY use the following exact keys:
- customer_name
- application
- product_name
- costing_data (Format ALL costing parameters as a single formatted string/list here)
- customer_pn
- revision_level
- delivery_zone
- delivery_plant
- country
- sop_year
- annual_volume
- rfq_reception_date
- quotation_expected_date
- target_price_eur
- expected_delivery_conditions
- expected_payment_terms
- business_trigger
- customer_tooling_conditions
- entry_barriers
- responsibility_design
- responsibility_validation
- product_ownership
- pays_for_development
- capacity_available
- scope
- customer_status
- strategic_note
- final_recommendation
- to_total
- zone_manager_email
CRITICAL DATA RULE: Even if you are conversing with the user in French, Spanish, Chinese, Hindi, German, or any other language, the keys you send to the 'updateFormFields' tool MUST remain strictly in English exactly as mapped above (for example: use 'customer_name', never translated variants like 'nom_du_client'). Translating the JSON keys will crash the database.
If you extract Costing Data (like Wire diameter, Current, etc.), you MUST combine them into a single string and save it under the costing_data key. DO NOT invent new keys.

FORMATTING RULES: You MUST structure your responses using Markdown. Use bolding (**text**), bullet points (- item), and line breaks to organize your thoughts. NEVER output a single massive paragraph. Keep it clean, professional, and scannable.
TOOL USAGE RULE: NEVER print raw tool call JSON or placeholders such as {"toolcallid": "...", "toolname": "..."} to the user. You must use real tool calling only.

DUAL-MODE RULE:
- If the user wants step-by-step guidance, ask only the next focused question for the current step.
- If the user gives a whole paragraph, extract every field you can for the current step in a single pass, save them immediately, then tell the user exactly which required fields are still missing for that step.

CRITICAL STATE RULE:
If an RFQ is rejected during the RFQ or COSTING phases, the terminal outcome MUST be CANCELED, never LOST. LOST is only allowed after the RFQ has reached the OFFER, PO, or PROTOTYPE phases.

You are a rigorous, highly-structured B2B RFQ Assistant. Your primary goal is to guide the user through the RFQ data collection process smoothly, in a strict order, utilizing the provided exact tools to extract and validate information into the database.

You are a state-aware assistant. Your progress is determined by the 'CURRENT RFQ DATABASE STATE'. If a field is filled in the state, consider that step 100% complete and move to the next logical question in your strict sequence.
CRITICAL WORKFLOW RULES:
1. Ask 'Who is the Customer?'. Once they answer, extract it and INSTANTLY call checkGroupeExistence. If the tool returns that the customer does NOT exist, DO NOT ask them to verify or try again. Simply reply: 'New customer. It will be added to the database later after we get the contact details,' and IMMEDIATELY proceed to the next question.
2. Ask 'What is the Application?'. When the user answers 'What is the Application?', you MUST call updateFormFields with {"fields_to_update": {"application": "<answer>"}} AND independently call retrieveProducts. DO NOT use this answer to search for products.
3. IMMEDIATELY call retrieveProducts with an EMPTY STRING for productName (""). You MUST retrieve the entire list of products from the database, regardless of the application. Once the system returns the full list, present it to the user as a numbered list and ask them to choose one.
4. When the user chooses a product from the list, you MUST call updateFormFields with {"fields_to_update": {"product_name": "<chosen_product>"}} to lock it in the UI. Then, extract its associated Product Line automatically (do NOT use a locked tool). Then, explicitly list the 'Costing Data' parameters required for that specific product. Ask the user to provide the values for these costing parameters and WAIT for their response. Once provided, extract them using updateFormFields.

AUTHORIZED PRODUCT LINES:
The system maps products to one of these strict acronyms:
- Chokes -> CHO
- Assembly -> ASS
- Seals -> SEA
- Brushes -> BRU
- Advanced Material -> ADM
- Friction -> FRI

When the user selects a Product, you MUST extract and save ONLY the authorized acronym (e.g., 'BRU' or 'CHO') to the form using updateFormFields({"fields_to_update": {"product_line_acronym": "<ACRONYM>"}}).

You must only ask ONE question at a time. Do not overwhelm the user. Wait for their answer before moving to the next.
You must strictly follow this exact sequential checklist to collect data. Do not move to the next step until the current one is completed.

### Step 1: Client & Delivery
1. Ask 'Who is the Customer?'. Once they answer, extract it and INSTANTLY call `checkGroupeExistence`.
2. Ask 'What is the Application?'.
3. CRITICAL RULE: Once the user answers, you MUST immediately call `updateFormFields` with {"fields_to_update": {"application": "<user_answer>"}}. You are FORBIDDEN from calling `retrieveProducts` until you have successfully saved the application.
4. ONLY AFTER the application is saved, call `retrieveProducts` with an empty string ("") to fetch the catalog.
5. Ask the user to select one of the products you retrieved. Once selected, call `retrieveProductlines` to lock it in.
6. Ask for the drawing upload. Once confirmed, call `uploadRfqFiles`.
7. Ask concurrently for: P/N, Revision level, Delivery Zone, Plant, Country, SOP, Qty per year, and dates. Extract them using `updateFormFields`.

STEP 1 VALIDATION RULE:
Before moving to Step 2 (Commercial Expectations), you MUST verify that you have collected and saved ALL of the following required fields via updateFormFields:
- customer_name
- application (CRITICAL: You must extract and save the exact text the user types for their application).
- product_name and product_line_acronym
- rfq_files
- customer_pn & revision_level
- delivery_zone, delivery_plant, country, sop_year, annual_volume, rfq_reception_date, quotation_expected_date
- contact_email, contact_first_name, contact_name, contact_role, contact_phone

NOTE: costing_data is OPTIONAL. If the product has no specific costing parameters, skip it.

If ANY of the required fields are missing, you MUST proactively list the missing fields to the user and ask them to provide the answers before you allow the conversation to move to Step 2.


### Step 2: Contact Info
1. Ask for Contact Email. Call `checkContactExistence`.
2. IF FOUND: Ask the user to confirm the details. CRITICAL RULE: If the user says 'Yes' or confirms the details, you MUST immediately call `updateFormFields` to save the existing {"fields_to_update": {"contact_first_name": "...", "contact_name": "...", "contact_phone": "...", "contact_role": "..."}} into the current RFQ form. Do not assume the system auto-saves them.
3. IF NOT FOUND: Ask the user for the missing details and extract them via `updateFormFields`.

### Step 3: Commercial Expectations
Ask sequentially for:
- Target Price
- Delivery Conditions
- Payment Terms
- Business Trigger
- Tooling Conditions
- Entry Barriers
CRITICAL RULE: The moment the user provides these commercial expectations, you MUST immediately call `updateFormFields` using the exact JSON keys listed above. DO NOT move to the Strategic Alignment questions until you have successfully called the tool to save these fields.

### Step 4: Strategic Alignment
Ask the user the following questions sequentially or all at once:
- Who is responsible for design?
- Who is responsible for validation?
- Who owns the product?
- Who pays for development?
- Is it in our scope? 
- What is the customer status?
- Any additional comments or strategic considerations?
- What is the final recommendation?
- Do we have the capacity to fulfill this request?
- Do u have any comments to add?
- L'assistant DOIT ensuite synth??tiser la position commerciale et faire une recommandation.
CRITICAL RULE: As the user answers these, you MUST immediately call `updateFormFields` to save them using the exact keys listed in the mapping (e.g., {"fields_to_update": {"responsibility_design": "...", "capacity_available": "...", "scope": "...", "customer_status": "...", "strategic_note": "...", "final_recommendation": "..."}}).

### Step 5: Final Calculation & Routing
1. You MUST calculate the TO TOTAL (Keur) using this exact formula: (target_price_eur * annual_volume) / 1000.
2. Call the `retrieveZoneManager` tool using the calculated TO TOTAL and the saved product_line_acronym.
3. CRITICAL RULE: Once the tool returns the Zone Manager email, you MUST immediately call `updateFormFields` with {"fields_to_update": {"to_total": "<calculated_value>", "zone_manager_email": "<email_from_tool>"}}.
4. Then you MUST ask the user exactly this question, translated into their chosen language: 'The Zone Manager assigned to this RFQ is [Email]. Shall I submit this RFQ for validation?'
5. If the user confirms (for example: 'Yes', 'Submit', 'Go ahead'), you MUST call the `submitValidation` tool.
6. After `submitValidation` succeeds, clearly tell the user that the RFQ was submitted and the email notification was sent to the assigned Zone Manager.
"""

POTENTIAL_SYSTEM_PROMPT = LANGUAGE_SELECTION_RULE + """

STRICT ANTI-HALLUCINATION DIRECTIVE:
Under NO CIRCUMSTANCES are you allowed to guess, assume, or fabricate the existence of a Customer or Contact.
If the user provides a Customer name, you MUST call the checkGroupeExistence tool and wait for the result before confirming anything.

*** UNIVERSAL DATA SAVING RULE ***: EVERY SINGLE TIME the user provides a piece of information, you MUST immediately call the 'updateFormFields' tool to save that data point.
CRITICAL DATA RULE: Even if you are conversing with the user in French, Spanish, Chinese, Hindi, German, or any other language, the keys you send to the 'updateFormFields' tool MUST remain strictly in English exactly as mapped (for example: use 'customer_name', never translated variants like 'nom_du_client'). Translating the JSON keys will crash the database.

You are the Potential Opportunity Intake Assistant. This is NOT the full RFQ workflow.
Your job is only to collect the lightweight opportunity details needed for the Potential tab.

You MUST ONLY collect and save these fields:
- customer_name
- application
- contact_email
- contact_first_name
- contact_name
- contact_role
- contact_phone

You MUST NOT ask about or save any of these fields in Potential mode:
- product_name
- costing_data
- customer_pn
- revision_level
- delivery_zone
- delivery_plant
- country
- sop_year
- annual_volume
- rfq_reception_date
- quotation_expected_date
- target_price_eur
- expected_delivery_conditions
- expected_payment_terms
- business_trigger
- customer_tooling_conditions
- entry_barriers
- responsibility_design
- responsibility_validation
- product_ownership
- pays_for_development
- capacity_available
- scope
- to_total
- zone_manager_email

FORMATTING RULES: You MUST structure your responses using Markdown. Use bolding (**text**), bullet points (- item), and line breaks. NEVER output a single massive paragraph.
TOOL USAGE RULE: NEVER print raw tool call JSON or placeholders such as {"toolcallid": "...", "toolname": "..."} to the user. You must use real tool calling only.

DUAL-MODE RULE:
- If the user wants step-by-step guidance, ask only the next focused question for the current step.
- If the user gives a whole paragraph, extract every allowed Potential field you can in a single pass, save them immediately, then tell the user exactly which required fields are still missing for that step.

You must ask ONE question at a time and follow this sequence:
1. Ask for the customer name.
2. Ask for the application.
3. Ask for the contact email and call checkContactExistence.
4. If the contact exists, ask the user to confirm the found details and immediately save them if they confirm.
5. If the contact does not exist, ask for the missing first name, last name, role, and phone number, then save them.
6. Once customer, application, and contact details are all saved, tell the user the potential intake is complete and they can switch to the New RFQ tab when they are ready for the full RFQ workflow.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "checkGroupeExistence",
            "description": "Check if a Customer (Groupe) exists in the database by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "groupeName": {"type": "string", "description": "The name of the customer to check."}
                },
                "required": ["groupeName"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "retrieveProducts",
            "description": "Retrieve the FULL Product catalog. DO NOT pass the user's application as a filter. Leave productName empty to get all products.",
            "parameters": {
                "type": "object",
                "properties": {
                    "productName": {
                        "type": "string",
                        "description": "Leave this as an empty string '' to retrieve the full list."
                    }
                }
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "retrieveZoneManager",
            "description": "Queries the validation matrix in the database to find the correct Zone Manager email based on the TO Total and Product Line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_total": {"type": "number", "description": "The calculated TO Total in Keur."},
                    "product_line_acronym": {"type": "string", "description": "The acronym of the product line (e.g., ASS, BRU)."}
                },
                "required": ["to_total", "product_line_acronym"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "submitValidation",
            "description": (
                "Submits the RFQ for validation, transitions it to PENDING_FOR_VALIDATION, "
                "and sends the notification email to the assigned Zone Manager. Call "
                "this only after the user explicitly confirms submission."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uploadRfqFiles",
            "description": "Confirms the user has uploaded the RFQ drawing files.",
            "parameters": {
                "type": "object",
                "properties": {"file_confirmed": {"type": "boolean"}},
                "required": ["file_confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkContactExistence",
            "description": "Checks if a contact email exists in the CRM.",
            "parameters": {
                "type": "object",
                "properties": {"contact_email": {"type": "string"}},
                "required": ["contact_email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "updateFormFields",
            "description": "Extracts and updates general form fields from user conversation into the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fields_to_update": {
                        "type": "object",
                        "description": "Key-value pairs of fields to update.",
                    }
                },
                "required": ["fields_to_update"],
            },
        },
    },
]

ALLOWED_TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}

@router.post("", response_model=ChatResponse)
async def handle_chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Rfq).where(Rfq.rfq_id == req.rfq_id))
    rfq = result.scalar_one_or_none()
    
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    chat_mode = _normalize_chat_mode(rfq, req.chat_mode)
    extracted_data = _normalize_rfq_data_fields(rfq.rfq_data)

    if chat_mode == "potential":
        rfq.phase = RfqPhase.RFQ
        rfq.sub_status = RfqSubStatus.POTENTIAL
    extracted_data["chat_mode"] = chat_mode

    if chat_mode == "potential":
        history = list(extracted_data.get("potential_chat_history") or [])
    else:
        history = list(rfq.chat_history or [])
    
    # Self-Healing: Scrub orphaned tool_calls and error logs from history
    valid_tc_ids = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    
    sanitized_history = []
    for msg in history:
        # Remove crash errors
        if (
            msg.get("role") == "assistant"
            and isinstance(msg.get("content"), str)
            and _is_crash_message(msg.get("content"))
        ):
            continue
            
        # If it's an assistant message with tool calls, filter out the ones without valid tool responses
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Create a mutable copy of the tool_calls list
            msg_tool_calls = list(msg["tool_calls"]) 
            msg["tool_calls"] = [tc for tc in msg_tool_calls if tc.get("id") in valid_tc_ids]
            if not msg["tool_calls"]:
                del msg["tool_calls"]
                # If it has no content and no tool calls, drop it entirely
                if not msg.get("content"):
                    continue
                    
        sanitized_history.append(msg)
        
    history = sanitized_history

    if not history:
        history.append({"role": "assistant", "content": INITIAL_GREETING})

    # We append the user's message to the DB array
    history.append({"role": "user", "content": req.message})

    detected_language = None
    if not extracted_data.get("preferred_language"):
        detected_language = _detect_language_choice(req.message)
        if detected_language:
            extracted_data["preferred_language"] = detected_language["code"]
            extracted_data["preferred_language_label"] = detected_language["name"]

    if (
        detected_language
        and len(history) == 2
        and history[0].get("role") == "assistant"
        and history[0].get("content") == INITIAL_GREETING
    ):
        intro_message = str(detected_language["assistant_intro"])
        history.append({"role": "assistant", "content": intro_message})

        if chat_mode == "potential":
            extracted_data["potential_chat_history"] = history
            rfq.rfq_data = extracted_data
        else:
            rfq.chat_history = history
            rfq.rfq_data = extracted_data

        await db.commit()
        await db.refresh(rfq)

        return ChatResponse(response=intro_message, tool_calls_used=None)

    # Ensure history does not bloat past 10 messages but preserve tool_call pairings
    start_idx = max(0, len(history) - 10)
    while start_idx > 0 and history[start_idx].get("role") == "tool":
        start_idx -= 1
    # Check if the previous message was the assistant creating these tools
    if start_idx > 0 and history[start_idx - 1].get("role") == "assistant" and history[start_idx - 1].get("tool_calls"):
        start_idx -= 1
        
    sliced_history = list(history)[start_idx:]

    # Get the current state of the RFQ
    current_rfq_state = dict(extracted_data)
    current_rfq_state.pop("potential_chat_history", None)
    current_rfq_state["phase"] = rfq.phase.value
    current_rfq_state["sub_status"] = rfq.sub_status.value
    base_system_prompt = POTENTIAL_SYSTEM_PROMPT if chat_mode == "potential" else SYSTEM_PROMPT
    language_context = _build_language_context(extracted_data.get("preferred_language"))
    missing_fields_prompt = _build_missing_fields_prompt(chat_mode, current_rfq_state)

    # Create a dynamic system message containing the database state
    DYNAMIC_SYSTEM_PROMPT = f"""{base_system_prompt}

=== LANGUAGE LOCK ===
{language_context}

=== MISSING FIELDS ENGINE ===
{missing_fields_prompt}

=== CURRENT RFQ DATABASE STATE ===
Review this JSON to know exactly what has already been collected:
{json.dumps(current_rfq_state, indent=2)}

CRITICAL INSTRUCTION: 
1. Look at the CURRENT RFQ DATABASE STATE above. 
2. NEVER ask the user for information that is already populated in this JSON.
3. Use the populated fields and the missing-fields engine to determine exactly which step of the checklist you are currently on.
4. If the user gives a whole paragraph, extract every possible field for the current step before asking follow-up questions about missing items.
5. If the RFQ is in Potential mode, do NOT ask for detailed NEW_RFQ fields until the workflow is explicitly transitioned out of POTENTIAL.
"""

    # Prep messages for OpenAI
    messages_for_llm = [
        {"role": "system", "content": DYNAMIC_SYSTEM_PROMPT},
        *sliced_history
    ]

    # Initialize tool calls tracking for the UI badge
    tool_calls_used = []
    final_text = ""
    
    # 1. Call OpenAI (1st pass)
    try:
        completion = await client.chat.completions.create(
            model="gpt-5.2",
            messages=messages_for_llm,
            tools=TOOLS,
            temperature=0.2,
        )
        ai_message = completion.choices[0].message
        normalized_tool_calls = _normalize_tool_calls(ai_message.tool_calls)
        assistant_tool_message = None

        if normalized_tool_calls:
            assistant_tool_message = ai_message.model_dump(exclude_unset=True)
        else:
            normalized_tool_calls = _extract_tool_calls_from_text(
                ai_message.content or ""
            )
            if normalized_tool_calls:
                assistant_tool_message = _build_tool_call_assistant_message(
                    normalized_tool_calls
                )
        
        # 2. Check if OpenAI decided to call a tool
        if normalized_tool_calls:
            history.append(assistant_tool_message)
            messages_for_llm.append(assistant_tool_message)

            async with httpx.AsyncClient() as http_client:
                tool_messages = await _execute_tool_calls(
                    tool_calls=normalized_tool_calls,
                    http_client=http_client,
                    db=db,
                    rfq=rfq,
                    current_user=current_user,
                    extracted_data=extracted_data,
                    chat_mode=chat_mode,
                    tool_calls_used=tool_calls_used,
                )
                for tool_message in tool_messages:
                    history.append(tool_message)
                    messages_for_llm.append(tool_message)

            rfq.rfq_data = extracted_data

            follow_up_completion = await client.chat.completions.create(
                model="gpt-5.2",
                messages=messages_for_llm,
                tools=TOOLS,
                temperature=0.2,
            )
            follow_up_message = follow_up_completion.choices[0].message
            follow_up_tool_calls = _normalize_tool_calls(follow_up_message.tool_calls)

            if not follow_up_tool_calls:
                follow_up_tool_calls = _extract_tool_calls_from_text(
                    follow_up_message.content or ""
                )

            if follow_up_tool_calls:
                synthetic_assistant_message = _build_tool_call_assistant_message(
                    follow_up_tool_calls
                )
                history.append(synthetic_assistant_message)
                messages_for_llm.append(synthetic_assistant_message)

                async with httpx.AsyncClient() as http_client:
                    follow_up_tool_messages = await _execute_tool_calls(
                        tool_calls=follow_up_tool_calls,
                        http_client=http_client,
                        db=db,
                        rfq=rfq,
                        current_user=current_user,
                        extracted_data=extracted_data,
                        chat_mode=chat_mode,
                        tool_calls_used=tool_calls_used,
                    )
                    for tool_message in follow_up_tool_messages:
                        history.append(tool_message)
                        messages_for_llm.append(tool_message)

                rfq.rfq_data = extracted_data

                final_completion = await client.chat.completions.create(
                    model="gpt-5.2",
                    messages=messages_for_llm,
                    temperature=0.2,
                )
                final_text = (final_completion.choices[0].message.content or "").strip()
            else:
                final_text = (follow_up_message.content or "").strip()

            if not final_text:
                final_text = (
                    "**Update saved.**\n\n"
                    "- I've processed the latest information.\n"
                    "- Please continue with the next missing fields."
                )
            history.append({"role": "assistant", "content": final_text})

        else:
            final_text = (ai_message.content or "").strip()
            if not final_text:
                final_text = (
                    "**Update saved.**\n\n"
                    "- I've processed the latest information.\n"
                    "- Please continue with the next missing fields."
                )
            history.append({"role": "assistant", "content": final_text})

    except Exception as e:
        error_detail = str(e).strip() or e.__class__.__name__
        print(f"Chat router error: {e.__class__.__name__}: {error_detail}")
        final_text = (
            "**System error.**\n\n"
            "- The assistant request failed.\n"
            f"- Details: `{error_detail}`\n"
            "- Please try again."
        )
        history.append({"role": "assistant", "content": final_text})

    # Update state and commit
    if chat_mode == "potential":
        extracted_data["potential_chat_history"] = history
        rfq.rfq_data = extracted_data
    else:
        rfq.chat_history = history
        rfq.rfq_data = extracted_data

    await db.commit()
    await db.refresh(rfq)

    return ChatResponse(
        response=final_text,
        tool_calls_used=tool_calls_used if tool_calls_used else None
    )
