import json
import logging
import datetime
import re
import unicodedata
import httpx
from openai import APITimeoutError, AsyncOpenAI
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, get_db3
from app.middleware.auth import get_current_user
from app.models.rfq import Rfq, RfqPhase, RfqSubStatus
from app.routers.rfq import (
    _assert_can_edit_base_rfq_data,
    _maybe_assign_systematic_rfq_id,
    _submit_rfq_for_validation_internal,
)
from app.services.routing import (
    APPROVED_DELIVERY_ZONES,
    N0_CEO_EMAIL,
    N1_VP_EMAIL,
    get_zone_manager_email,
    normalize_delivery_zone,
)
from app.models.user import User
from app.models.validation_matrix import ValidationMatrix
from app.utils.currency import get_eur_exchange_rate

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)

OPENAI_TIMEOUT_SECONDS = 180.0
INTERNAL_TOOL_TIMEOUT_SECONDS = 90.0

# Async OpenAI client
client = AsyncOpenAI(
    api_key=settings.OPENAI_API_KEY or "dummy_key",
    http_client=httpx.AsyncClient(timeout=httpx.Timeout(OPENAI_TIMEOUT_SECONDS)),
)

BASE_URL = "https://rfq-api.azurewebsites.net"
INITIAL_GREETING = (
    "Please select your preferred language.\n"
    "1- English\n"
    "2- FranÃ§ais\n"
    "3- ä¸­æ–‡\n"
    "4- EspaÃ±ol\n"
    "5- Deutsch\n"
    "6- à¤¹à¤¿à¤¨à¥à¤¦à¥€"
)
LANGUAGE_SELECTION_RULE = (
    "CRITICAL RULE: The user will respond to the initial greeting with a language "
    "choice (either by number or name). You MUST instantly acknowledge their choice "
    "in that specific language. From that point forward, conduct the ENTIRE "
    "conversation, including all step-by-step guidance and missing field "
    "announcements, strictly in their chosen language."
)
STATE_RECONCILIATION_DIRECTIVE = """
CRITICAL DIRECTIVE - PROACTIVE DATA EXTRACTION:
1. You are an automated data extraction engine. You MUST NOT wait for the user to ask you to save or update the form.
2. STATE RECONCILIATION: On every single turn, you must compare the recent conversation history against the 'CURRENT RFQ DATABASE STATE'. 
3. If the user has provided ANY valid information in the recent chat history that is currently missing or empty in the database state, your IMMEDIATE and ONLY next action MUST be to call the 'updateFormFields' tool to save that data.
4. Do not acknowledge the user's answer in text without ALSO calling the tool in the same response.
"""
ENGLISH_INITIAL_GREETING = (
    "Hello, I'm your sales assistant. I'll be helping you fill your RFQ. "
    "How would you like to proceed?\n"
    "1. Guide me step by step\n"
    "2. I will provide a whole paragraph"
)
SELF_REVISION_REQUEST_COMMENT = "Self-update initiated by assigned validator."
SELF_REVISION_GREETING = (
    "Welcome back! Please tell me what fields you would like to update."
)
ENGLISH_ONLY_RULE = (
    "CRITICAL RULE: The conversation MUST be strictly in English. Whenever you give "
    "the user a choice or ask a multiple-choice question, you MUST format it as a "
    "numbered list (1, 2, 3...). You must be able to understand and proceed if the "
    "user simply replies with the number of their choice. CRITICAL LANGUAGE RULE: "
    "You are strictly forbidden from using any language other than English in your "
    "text responses. Do not use Russian, French, or any other foreign words "
    "(for example: never use 'ÑƒÑ‚Ð¾Ñ‡Ð½Ð¸Ñ‚ÑŒ'; use 'clarify' or 'specify')."
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
        "name": "FranÃ§ais",
        "aliases": {"2", "franÃ§ais", "francais", "french", "fr"},
        "assistant_intro": (
            "Bonjour, je suis votre assistant commercial. Je vais vous aider Ã  "
            "remplir votre RFQ. Souhaitez-vous que je vous guide Ã©tape par Ã©tape, "
            "ou prÃ©fÃ©rez-vous me donner un paragraphe complet afin que j'extraie "
            "les champs nÃ©cessaires pour chaque Ã©tape et que je vous indique les "
            "informations manquantes ?"
        ),
    },
    "zh": {
        "menu_number": "3",
        "name": "ä¸­æ–‡",
        "aliases": {"3", "ä¸­æ–‡", "chinese", "mandarin", "zh"},
        "assistant_intro": (
            "æ‚¨å¥½ï¼Œæˆ‘æ˜¯æ‚¨çš„é”€å”®åŠ©æ‰‹ã€‚æˆ‘å°†å¸®åŠ©æ‚¨å¡«å†™ RFQã€‚æ‚¨å¸Œæœ›æˆ‘ä¸€æ­¥ä¸€æ­¥å¼•å¯¼æ‚¨ï¼Œ"
            "è¿˜æ˜¯æ‚¨å¯ä»¥ç›´æŽ¥ç»™æˆ‘ä¸€æ•´æ®µæ–‡å­—ï¼Œæˆ‘ä¼šä¸ºæ¯ä¸ªæ­¥éª¤æå–æ‰€éœ€å­—æ®µå¹¶å‘Šè¯‰æ‚¨"
            "è¿˜ç¼ºå°‘å“ªäº›ä¿¡æ¯ï¼Ÿ"
        ),
    },
    "es": {
        "menu_number": "4",
        "name": "EspaÃ±ol",
        "aliases": {"4", "espaÃ±ol", "espanol", "spanish", "es"},
        "assistant_intro": (
            "Hola, soy su asistente comercial. Le ayudarÃ© a completar su RFQ. "
            "Â¿Quiere que le guÃ­e paso a paso, o puede darme un pÃ¡rrafo completo y "
            "yo extraerÃ© los campos necesarios para cada etapa y le dirÃ© quÃ© "
            "informaciÃ³n falta?"
        ),
    },
    "de": {
        "menu_number": "5",
        "name": "Deutsch",
        "aliases": {"5", "deutsch", "german", "de"},
        "assistant_intro": (
            "Hallo, ich bin Ihr Vertriebsassistent. Ich helfe Ihnen beim AusfÃ¼llen "
            "Ihrer RFQ. MÃ¶chten Sie, dass ich Sie Schritt fÃ¼r Schritt fÃ¼hre, oder "
            "kÃ¶nnen Sie mir einen ganzen Absatz geben und ich extrahiere die "
            "benÃ¶tigten Felder fÃ¼r jeden Schritt und teile Ihnen mit, welche "
            "Informationen noch fehlen?"
        ),
    },
    "hi": {
        "menu_number": "6",
        "name": "à¤¹à¤¿à¤¨à¥à¤¦à¥€",
        "aliases": {"6", "à¤¹à¤¿à¤¨à¥à¤¦à¥€", "à¤¹à¤¿à¤‚à¤¦à¥€", "hindi", "hi"},
        "assistant_intro": (
            "à¤¨à¤®à¤¸à¥à¤¤à¥‡, à¤®à¥ˆà¤‚ à¤†à¤ªà¤•à¤¾ à¤¸à¥‡à¤²à¥à¤¸ à¤…à¤¸à¤¿à¤¸à¥à¤Ÿà¥‡à¤‚à¤Ÿ à¤¹à¥‚à¤à¥¤ à¤®à¥ˆà¤‚ à¤†à¤ªà¤•à¤¾ RFQ à¤­à¤°à¤¨à¥‡ à¤®à¥‡à¤‚ à¤®à¤¦à¤¦ à¤•à¤°à¥‚à¤à¤—à¤¾à¥¤ "
            "à¤•à¥à¤¯à¤¾ à¤†à¤ª à¤šà¤¾à¤¹à¤¤à¥‡ à¤¹à¥ˆà¤‚ à¤•à¤¿ à¤®à¥ˆà¤‚ à¤†à¤ªà¤•à¥‹ à¤šà¤°à¤£-à¤¦à¤°-à¤šà¤°à¤£ à¤®à¤¾à¤°à¥à¤—à¤¦à¤°à¥à¤¶à¤¨ à¤¦à¥‚à¤, à¤¯à¤¾ à¤†à¤ª à¤®à¥à¤à¥‡ "
            "à¤à¤• à¤ªà¥‚à¤°à¤¾ à¤ªà¥ˆà¤°à¤¾à¤—à¥à¤°à¤¾à¤« à¤¦à¥‡ à¤¸à¤•à¤¤à¥‡ à¤¹à¥ˆà¤‚ à¤”à¤° à¤®à¥ˆà¤‚ à¤¹à¤° à¤šà¤°à¤£ à¤•à¥‡ à¤²à¤¿à¤ à¤†à¤µà¤¶à¥à¤¯à¤• à¤«à¤¼à¥€à¤²à¥à¤¡ à¤¨à¤¿à¤•à¤¾à¤²à¤•à¤° "
            "à¤¬à¤¤à¤¾à¤Šà¤à¤—à¤¾ à¤•à¤¿ à¤•à¥Œà¤¨-à¤¸à¥€ à¤œà¤¾à¤¨à¤•à¤¾à¤°à¥€ à¤…à¤­à¥€ à¤¬à¤¾à¤•à¥€ à¤¹à¥ˆ?"
        ),
    },
}
POTENTIAL_ALLOWED_FIELDS = {
    "customer_name",
    "application",
    "contact_email",
    "contact_name",
    "contact_role",
    "contact_phone",
}
RFQ_ALLOWED_FIELDS = POTENTIAL_ALLOWED_FIELDS | {
    "product_name",
    "product_line_acronym",
    "project_name",
    "costing_data",
    "customer_pn",
    "revision_level",
    "delivery_zone",
    "delivery_plant",
    "country",
    "po_date",
    "ppap_date",
    "sop_year",
    "annual_volume",
    "rfq_reception_date",
    "quotation_expected_date",
    "target_price_eur",
    "target_price_local",
    "target_price_currency",
    "target_price_is_estimated",
    "target_price_note",
    "expected_delivery_conditions",
    "expected_payment_terms",
    "type_of_packaging",
    "business_trigger",
    "customer_tooling_conditions",
    "entry_barriers",
    "responsibility_design",
    "responsibility_validation",
    "product_ownership",
    "pays_for_development",
    "capacity_available",
    "scope",
    "strategic_note",
    "final_recommendation",
    "to_total",
    "to_total_local",
    "zone_manager_email",
    "validator_role",
}
UPDATE_FORM_FIELD_ALIASES = {
    "customerName": "customer_name",
    "productName": "product_name",
    "productLineAcronym": "product_line_acronym",
    "projectName": "project_name",
    "customerPn": "customer_pn",
    "revisionLevel": "revision_level",
    "deliveryZone": "delivery_zone",
    "deliveryPlant": "delivery_plant",
    "contactEmail": "contact_email",
    "contactName": "contact_name",
    "contactRole": "contact_role",
    "contactPhone": "contact_phone",
    "targetPriceEur": "target_price_eur",
    "targetPriceLocal": "target_price_local",
    "targetPriceCurrency": "target_price_currency",
    "targetPriceIsEstimated": "target_price_is_estimated",
    "targetPriceNote": "target_price_note",
    "expectedDeliveryConditions": "expected_delivery_conditions",
    "expectedPaymentTerms": "expected_payment_terms",
    "typeOfPackaging": "type_of_packaging",
    "businessTrigger": "business_trigger",
    "customerToolingConditions": "customer_tooling_conditions",
    "entryBarriers": "entry_barriers",
    "responsibilityDesign": "responsibility_design",
    "responsibilityValidation": "responsibility_validation",
    "productOwnership": "product_ownership",
    "paysForDevelopment": "pays_for_development",
    "capacityAvailable": "capacity_available",
    "strategicNote": "strategic_note",
    "finalRecommendation": "final_recommendation",
    "toTotal": "to_total",
    "toTotalLocal": "to_total_local",
    "zoneManagerEmail": "zone_manager_email",
    "validatorRole": "validator_role",
    "poDate": "po_date",
    "ppapDate": "ppap_date",
    "rfqReceptionDate": "rfq_reception_date",
    "quotationExpectedDate": "quotation_expected_date",
    "annualVolume": "annual_volume",
    "sopYear": "sop_year",
}
UPDATE_FORM_FIELD_PROPERTIES = {
    field_name: {"type": "string"}
    for field_name in sorted(RFQ_ALLOWED_FIELDS)
}
UPDATE_FORM_FIELD_PROPERTIES["to_total"] = {"type": "number"}
UPDATE_FORM_FIELD_PROPERTIES["to_total_local"] = {"type": "number"}
UPDATE_FORM_FIELD_PROPERTIES["target_price_is_estimated"] = {"type": "boolean"}
AI_GENERATED_STEP_FIELDS = [
    "to_total",
    "zone_manager_email",
    "validator_email",
    "validator_role",
]
POTENTIAL_STEPS: list[tuple[int, list[str]]] = [
    (1, ["customer_name", "application"]),
    (2, ["contact_email"]),
    (3, ["contact_name", "contact_role", "contact_phone"]),
]
RFQ_STEPS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            "customer_name",
            "application",
            "product_name",
            "product_line_acronym",
            "project_name",
            "rfq_files",
            "customer_pn",
            "revision_level",
            "delivery_zone",
            "delivery_plant",
            "country",
            "po_date",
            "ppap_date",
            "sop_year",
            "annual_volume",
            "rfq_reception_date",
            "quotation_expected_date",
            "contact_email",
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
            "type_of_packaging",
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
            "strategic_note",
            "final_recommendation",
        ],
    ),
    (4, ["to_total", "zone_manager_email", "validator_role"]),
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


def _coerce_numeric_value(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value or "").replace(",", ""))
    if not cleaned:
        raise ValueError("Numeric value is missing.")
    return float(cleaned)


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


def _is_revision_requested(rfq: Rfq) -> bool:
    return (
        rfq.phase == RfqPhase.RFQ
        and rfq.sub_status == RfqSubStatus.REVISION_REQUESTED
    )


def _is_self_revision_note(revision_notes: str | None) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(revision_notes or "").casefold()).strip()
    return normalized == "self update initiated by assigned validator"


def _build_revision_greeting(revision_notes: str | None) -> str:
    notes = str(revision_notes or "").strip()
    if _is_self_revision_note(notes) or not notes:
        return SELF_REVISION_GREETING
    return (
        "The validator has requested the following updates: "
        f"{notes}. What would you like me to change?"
    )


def _build_revision_mode_prompt_context(rfq: Rfq) -> str:
    revision_notes = str(rfq.revision_notes or "").strip()
    if not _is_revision_requested(rfq):
        return (
            "REVISION MODE CONTEXT:\n"
            "- The RFQ is not in revision mode.\n"
            "- Follow the normal RFQ workflow rules.\n"
        )

    if _is_self_revision_note(revision_notes):
        return f"""REVISION MODE CONTEXT:
- The RFQ sub_status is REVISION_REQUESTED.
- revision_notes: {json.dumps(revision_notes)}
- The RFQ is in Revision Mode and remains fully editable through updateFormFields.
- DO NOT say that updates are limited to NEW_RFQ. REVISION_REQUESTED is also a valid editable RFQ state.
- The user may update fields that are already populated. Treat the revision request as permission to overwrite existing values.
- Self-update rule: greet the user with exactly "{SELF_REVISION_GREETING}"
- Do NOT mention the validator in this self-update flow.
- When the RFQ is in REVISION_REQUESTED, you are ONLY responsible for updating requested fields.
- DO NOT call submitValidation or any other tool that changes RFQ status.
- When the user says the updates are complete, instruct them to click the physical "Submit Updates" button at the top of their screen to send the RFQ back to the validator.
"""

    if revision_notes:
        return f"""REVISION MODE CONTEXT:
- The RFQ sub_status is REVISION_REQUESTED.
- revision_notes: {json.dumps(revision_notes)}
- The RFQ is in Revision Mode and remains fully editable through updateFormFields.
- DO NOT say that updates are limited to NEW_RFQ. REVISION_REQUESTED is also a valid editable RFQ state.
- The user may update fields that are already populated. Treat the validator feedback as permission to overwrite existing values.
- External revision rule: summarize the validator feedback and greet the user with exactly "{_build_revision_greeting(revision_notes)}"
- You must guide the user based on the validator feedback above, even when the referenced fields are already populated in the database state.
- When the RFQ is in REVISION_REQUESTED, you are ONLY responsible for updating requested fields.
- DO NOT call submitValidation or any other tool that changes RFQ status.
- When the user says the updates are complete, instruct them to click the physical "Submit Updates" button at the top of their screen to send the RFQ back to the validator.
"""

    return f"""REVISION MODE CONTEXT:
- The RFQ sub_status is REVISION_REQUESTED.
- revision_notes: {json.dumps(revision_notes)}
- The RFQ is in Revision Mode and remains fully editable through updateFormFields.
- DO NOT say that updates are limited to NEW_RFQ. REVISION_REQUESTED is also a valid editable RFQ state.
- Greet the user with exactly "{SELF_REVISION_GREETING}"
- When the RFQ is in REVISION_REQUESTED, you are ONLY responsible for updating requested fields.
- DO NOT call submitValidation or any other tool that changes RFQ status.
- When the user says the updates are complete, instruct them to click the physical "Submit Updates" button at the top of their screen to send the RFQ back to the validator.
"""


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
    steps = POTENTIAL_STEPS if chat_mode == "potential" else RFQ_STEPS
    current_step, missing_fields = _get_current_step_and_missing_fields(chat_mode, data)
    current_step_fields = next(
        (fields for step_number, fields in steps if step_number == current_step),
        [],
    )
    filled_fields = [field for field in current_step_fields if field not in missing_fields]
    user_keys_missing = [key for key in missing_fields if key not in AI_GENERATED_STEP_FIELDS]
    ai_keys_missing = [key for key in missing_fields if key in AI_GENERATED_STEP_FIELDS]
    prompt = (
        f"STATE RECONCILIATION FOR STEP {current_step}:\n"
        f"- Fields already present and MUST NOT be asked again: {filled_fields}\n"
        f"- Missing fields you must ASK THE USER for: {user_keys_missing}\n"
        f"- Missing fields YOU MUST GENERATE/CALCULATE yourself: {ai_keys_missing}"
    )
    if not missing_fields:
        prompt += (
            "\nAll required fields for this step are already present, so move to the next "
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
        normalized["product_line_acronym"] = (
            normalized.get("product_line_acronym")
            or normalized.get("productLineAcronym")
            or normalized.get("productLine")
            or normalized.get("product_line")
        )
        normalized["delivery_zone"] = (
            normalized.get("delivery_zone")
            or normalized.get("deliveryZone")
            or normalized.get("zone")
        )
    elif func_name == "get_eur_exchange_rate":
        normalized["currency_code"] = (
            normalized.get("currency_code")
            or normalized.get("currencyCode")
            or normalized.get("currency")
            or normalized.get("from_currency")
            or normalized.get("fromCurrency")
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
        normalized_fields = {}
        if isinstance(fields, dict):
            for key, value in fields.items():
                normalized_fields[UPDATE_FORM_FIELD_ALIASES.get(key, key)] = value
        normalized = {"fields_to_update": normalized_fields}
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


def _extract_tool_calls_from_text(
    content: str,
    allowed_tool_names: set[str] | None = None,
) -> list[dict]:
    parsed_payload = _try_load_json_payload(content)
    if parsed_payload is None:
        return []

    allowed_names = allowed_tool_names or ALLOWED_TOOL_NAMES

    if isinstance(parsed_payload, dict) and "tool_calls" in parsed_payload:
        raw_calls = parsed_payload.get("tool_calls") or []
    elif isinstance(parsed_payload, dict) and "tooluses" in parsed_payload:
        raw_calls = parsed_payload.get("tooluses") or []
    elif isinstance(parsed_payload, dict) and "tool_uses" in parsed_payload:
        raw_calls = parsed_payload.get("tool_uses") or []
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
        recipient_name = item.get("recipientname") or item.get("recipient_name")
        if not func_name and isinstance(recipient_name, str):
            func_name = recipient_name.split(".")[-1]
        if not func_name and isinstance(item.get("function"), dict):
            func_name = item["function"].get("name")

        if func_name not in allowed_names:
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


def _payload_contains_internal_tool_markers(payload) -> bool:
    marker_keys = {
        "tooluses",
        "tool_uses",
        "tool_calls",
        "recipientname",
        "recipient_name",
        "toolcallid",
        "tool_call_id",
        "toolname",
        "tool_name",
    }
    if isinstance(payload, dict):
        lowered_keys = {str(key).casefold() for key in payload.keys()}
        if marker_keys & lowered_keys:
            return True
        return any(_payload_contains_internal_tool_markers(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_internal_tool_markers(item) for item in payload)
    return False


def _get_available_tools(rfq: Rfq) -> list[dict]:
    if _is_revision_requested(rfq):
        return [
            tool
            for tool in TOOLS
            if tool["function"]["name"] != "submitValidation"
        ]
    return TOOLS


def _is_internal_tool_payload_text(content: str) -> bool:
    text = _strip_fenced_payload(content)
    if not text:
        return False

    lowered = text.lstrip().casefold()
    if lowered.startswith('{"tooluses":') or lowered.startswith('{"tool_uses":'):
        return True

    parsed_payload = _try_load_json_payload(text)
    if parsed_payload is None:
        return False
    return _payload_contains_internal_tool_markers(parsed_payload)


def _dedupe_adjacent_blocks(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks:
        return ""

    deduped_blocks: list[str] = []
    previous_normalized = ""
    for block in blocks:
        normalized = re.sub(r"\s+", " ", block).strip().casefold()
        if normalized and normalized == previous_normalized:
            continue
        deduped_blocks.append(block)
        previous_normalized = normalized

    return "\n\n".join(deduped_blocks).strip()


def _sanitize_assistant_text(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    if _is_internal_tool_payload_text(text):
        return ""
    return _dedupe_adjacent_blocks(text)


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


def _sanitize_chat_history(history: list[dict] | None) -> list[dict]:
    raw_history = list(history or [])
    valid_tc_ids = {
        message.get("tool_call_id")
        for message in raw_history
        if isinstance(message, dict) and message.get("role") == "tool"
    }

    sanitized_history = []
    for message in raw_history:
        if not isinstance(message, dict):
            continue

        next_message = dict(message)
        content = next_message.get("content")

        if (
            next_message.get("role") == "assistant"
            and isinstance(content, str)
            and _is_crash_message(content)
        ):
            continue

        if (
            next_message.get("role") == "assistant"
            and isinstance(content, str)
            and _is_internal_tool_payload_text(content)
        ):
            continue

        tool_calls = next_message.get("tool_calls")
        if next_message.get("role") == "assistant" and tool_calls:
            filtered_tool_calls = [
                tool_call
                for tool_call in list(tool_calls)
                if tool_call.get("id") in valid_tc_ids
            ]
            if filtered_tool_calls:
                next_message["tool_calls"] = filtered_tool_calls
            else:
                next_message.pop("tool_calls", None)
                if not next_message.get("content"):
                    continue

        sanitized_history.append(next_message)

    return sanitized_history


def _map_visible_chat_entries(history: list[dict]) -> list[dict]:
    visible_entries = []
    for raw_index, entry in enumerate(history):
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


async def _execute_tool_calls(
    *,
    tool_calls: list[dict],
    http_client: httpx.AsyncClient,
    db: AsyncSession,
    db3: AsyncSession,
    rfq: Rfq,
    current_user: User,
    extracted_data: dict,
    chat_mode: str,
    tool_calls_used: list[str],
) -> tuple[list[dict], bool]:
    tool_messages: list[dict] = []
    auto_redirect = False

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
            # Only use the boolean True as a fallback if the array is empty.
            # If the upload endpoint already populated the array, DO NOT
            # overwrite it so the Azure metadata survives.
            current_files = extracted_data.get("rfq_files")
            if not isinstance(current_files, list) or len(current_files) == 0:
                extracted_data["rfq_files"] = True

        elif func_name == "checkContactExistence":
            email = args.get("contact_email")
            if email:
                extracted_data["contact_email"] = email
            resp = await http_client.get(
                f"{BASE_URL}/api/contact/check",
                params={"email": email},
            )
            tool_response_text = resp.text

        elif func_name == "get_eur_exchange_rate":
            currency_code = str(args.get("currency_code") or "").strip().upper()
            eur_rate = await get_eur_exchange_rate(currency_code, db3=db3)
            fallback_used = bool(
                currency_code and currency_code != "EUR" and eur_rate == 1.0
            )
            if fallback_used:
                logger.warning(
                    "FX lookup for %s fell back to 1.0 during chat tool execution.",
                    currency_code,
                )
            tool_response_text = json.dumps(
                {
                    "currency_code": currency_code,
                    "eur_rate": eur_rate,
                    "fallback_used": fallback_used,
                }
            )

        elif func_name == "retrieveZoneManager":
            acronym = (
                str(
                    args.get("product_line_acronym")
                    or extracted_data.get("product_line_acronym")
                    or rfq.product_line_acronym
                    or ""
                ).strip()
            )
            raw_delivery_zone = (
                args.get("delivery_zone")
                or extracted_data.get("delivery_zone")
                or ""
            )

            try:
                annual_volume_value = extracted_data.get("annual_volume")
                target_price_value = extracted_data.get("target_price_eur")
                if annual_volume_value in (None, ""):
                    raise ValueError(
                        "annual_volume must be saved before validator routing."
                    )
                if target_price_value in (None, ""):
                    raise ValueError(
                        "target_price_eur must be saved before validator routing."
                    )

                volume = _coerce_numeric_value(annual_volume_value)
                price = _coerce_numeric_value(target_price_value)
                to_total_float = (volume * price) / 1000.0
                extracted_data["to_total"] = str(to_total_float)

                # Also compute to_total_local for the user-facing UI
                local_price_value = extracted_data.get("target_price_local")
                if local_price_value not in (None, ""):
                    try:
                        local_price = _coerce_numeric_value(local_price_value)
                        to_total_local_float = (volume * local_price) / 1000.0
                        extracted_data["to_total_local"] = str(to_total_local_float)
                    except (ValueError, TypeError):
                        pass
                query = select(ValidationMatrix).where(
                    ValidationMatrix.acronym == acronym
                )
                result = await db.execute(query)
                matrix = result.scalar_one_or_none()

                if matrix:
                    if to_total_float <= matrix.n3_kam_limit:
                        required_role = "Commercial"
                    elif to_total_float <= matrix.n2_zone_limit:
                        required_role = "Zone Manager"
                    elif to_total_float <= matrix.n1_vp_limit:
                        required_role = "VP Sales"
                    else:
                        required_role = "CEO"

                    zone_manager_email = None
                    canonical_delivery_zone = normalize_delivery_zone(
                        raw_delivery_zone
                    )

                    if required_role == "Commercial":
                        zone_manager_email = rfq.created_by_email
                    elif required_role == "VP Sales":
                        zone_manager_email = N1_VP_EMAIL
                    elif required_role == "CEO":
                        zone_manager_email = N0_CEO_EMAIL
                    elif required_role == "Zone Manager":
                        zone_manager_email, canonical_delivery_zone = (
                            get_zone_manager_email(raw_delivery_zone)
                        )
                        if not zone_manager_email:
                            tool_response_text = json.dumps(
                                {
                                    "error": (
                                        f"Unknown delivery zone '{raw_delivery_zone}'. "
                                        "delivery_zone must be one of: asie est, "
                                        "asie sud, europe, amerique."
                                    ),
                                    "to_total": to_total_float,
                                    "delivery_zone": raw_delivery_zone,
                                    "approved_delivery_zones": list(
                                        APPROVED_DELIVERY_ZONES
                                    ),
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
                            continue

                    extracted_data["validator_role"] = required_role
                    if canonical_delivery_zone:
                        extracted_data["delivery_zone"] = canonical_delivery_zone
                    if zone_manager_email:
                        extracted_data["zone_manager_email"] = zone_manager_email

                    tool_response_text = json.dumps(
                        {
                            "role_assigned": required_role,
                            "validator_role": required_role,
                            "validator_email": zone_manager_email,
                            "zone_manager_email": zone_manager_email,
                            "to_total": to_total_float,
                            "to_total_local": extracted_data.get("to_total_local"),
                            "delivery_zone": canonical_delivery_zone,
                        }
                    )
                else:
                    tool_response_text = json.dumps(
                        {
                            "to_total": to_total_float,
                            "error": (
                                f"Product line '{acronym}' not found in validation matrix."
                            )
                        }
                    )
            except Exception as e:
                tool_response_text = json.dumps({"error": str(e)})

        elif func_name == "submitValidation":
            if _is_revision_requested(rfq):
                tool_response_text = json.dumps(
                    {
                        "success": False,
                        "error": (
                            "submitValidation is not available while the RFQ is in "
                            "REVISION_REQUESTED. Update the requested fields only, then ask "
                            "the user to click the physical 'Submit Updates' button at the "
                            "top of the screen."
                        ),
                        "status_code": 409,
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
                continue
            try:
                saved_data = _normalize_rfq_data_fields(rfq.rfq_data)
                validator_email = str(saved_data.get("zone_manager_email") or rfq.zone_manager_email or "").strip()
                to_total_value = str(saved_data.get("to_total") or "").strip()
                product_line_acronym = str(
                    saved_data.get("product_line_acronym")
                    or rfq.product_line_acronym
                    or ""
                ).strip()

                missing_step4_fields = [
                    field_name
                    for field_name, field_value in (
                        ("to_total", to_total_value),
                        ("zone_manager_email", validator_email),
                        ("product_line_acronym", product_line_acronym),
                    )
                    if not field_value
                ]

                if missing_step4_fields:
                    tool_response_text = json.dumps(
                        {
                            "success": False,
                            "error": (
                                "Step 4 data must be saved with updateFormFields before "
                                "submitValidation can run."
                            ),
                            "missing_fields": missing_step4_fields,
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
                    continue

                is_self_validator = str(validator_email).strip().casefold() == current_user.email.casefold()
                submit_result = await _submit_rfq_for_validation_internal(
                    rfq=rfq,
                    db=db,
                    current_user=current_user,
                    send_email=not is_self_validator,
                )
                await db.refresh(rfq)
                extracted_data.clear()
                extracted_data.update(_normalize_rfq_data_fields(rfq.rfq_data))
                auto_redirect = auto_redirect or is_self_validator
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
                        "validator_role": extracted_data.get("validator_role"),
                        "email_sent": submit_result.get("email_sent", not is_self_validator),
                        "auto_redirect": is_self_validator,
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
            raw_fields = args.get("fields_to_update", {})
            fields = dict(raw_fields) if isinstance(raw_fields, dict) else {}
            if "target_price_is_estimated" in fields:
                val = fields["target_price_is_estimated"]
                fields["target_price_is_estimated"] = (
                    val if isinstance(val, bool)
                    else str(val).strip().lower() in ("true", "1", "yes")
                )
            filtered_fields = _filter_update_fields(chat_mode, fields)
            if "delivery_zone" in filtered_fields:
                canonical_delivery_zone = normalize_delivery_zone(
                    filtered_fields.get("delivery_zone")
                )
                if canonical_delivery_zone:
                    filtered_fields["delivery_zone"] = canonical_delivery_zone
            for key, value in filtered_fields.items():
                if key == "target_price_is_estimated":
                    extracted_data[key] = bool(value)
                else:
                    extracted_data[key] = str(value)
            if "zone_manager_email" in filtered_fields:
                rfq.zone_manager_email = str(filtered_fields.get("zone_manager_email") or "").strip() or None
            if "product_line_acronym" in filtered_fields:
                rfq.product_line_acronym = str(filtered_fields.get("product_line_acronym") or "").strip() or None
            rfq.rfq_data = await _maybe_assign_systematic_rfq_id(
                db,
                rfq,
                dict(extracted_data),
            )
            extracted_data.clear()
            extracted_data.update(_normalize_rfq_data_fields(rfq.rfq_data))
            await db.flush()
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

    return tool_messages, auto_redirect

class ChatRequest(BaseModel):
    rfq_id: str
    message: str
    chat_mode: str = "rfq"


class ChatEditRequest(BaseModel):
    rfq_id: str
    visible_message_index: int
    message: str


class ChatResponse(BaseModel):
    response: str
    tool_calls_used: list[str] | None = None
    auto_redirect: bool | None = None

SYSTEM_PROMPT = STATE_RECONCILIATION_DIRECTIVE + "\n" + ENGLISH_ONLY_RULE + """

CRITICAL CONVERSATION RULES:
1. NO CHATTER: Never say 'Update saved', 'I have processed your request', or 'Got it'. Just ask the exact next missing question.
2. NO UNSOLICITED SUMMARIES: Never print out a summary of the RFQ fields unless the user explicitly types 'summary'.
3. STRICT ENGLISH TRANSLATION: You must seamlessly translate all delivery zones, regions, and countries into English before saving them. You MUST adhere to the following exact mappings for delivery zones:
   - "asie sud" MUST be saved as "South Asia"
   - "asie est" MUST be saved as "East Asia"
   - "amerique" MUST be saved as "America"
   - "europe" MUST be saved as "Europe"

STRICT ANTI-HALLUCINATION DIRECTIVE:
Under NO CIRCUMSTANCES are you allowed to guess, assume, or fabricate the existence of a Customer, Product, Product Line, or Contact.
If the user provides a Customer name, you MUST call the checkGroupeExistence tool. YOU MUST WAIT for the system to return the JSON response containing the database result.
DO NOT generate a text response confirming or denying the customer until you have physically received the tool_call_id response from the system. If you violate this rule, the system will fail.

*** UNIVERSAL DATA SAVING RULE ***: EVERY SINGLE TIME the user provides a piece of information (e.g., Application, chosen Product Name, Contact Info, Target Price, Quantities, Dates, etc.), you MUST immediately call the 'updateFormFields' tool to save that specific data point to the database. You can call 'updateFormFields' at the exact same time as you ask your next question. If you fail to call 'updateFormFields', the UI will break.
USER-FACING TERMINOLOGY RULE: When speaking to the user, you MUST always say 'Validator' and NEVER say 'Zone Manager'. This terminology rule applies to every user-facing sentence, confirmation, and question.
DATE FORMAT RULE: When updating date fields, you must output the strict YYYY-MM-DD format. If the user only provides a month and year (for example, "June 2025"), default to the 1st of the month (for example, "2025-06-01"). This applies to po_date, ppap_date, rfq_reception_date, and quotation_expected_date.
NUMBERED OPTION FORMATTING RULE: When asking the user a question with multiple choices, you MUST NEVER number the question itself. You must ask the question on a new line, and then start the numbered list of choices starting at number 1. (Example: "Shall I submit this now?\n1. Yes\n2. No")
NUMBERED OPTION PARSING RULE: When you provide a numbered list of options and the user replies with a single number, you MUST internally substitute that number with the exact text of the corresponding option before taking any further action or making tool calls. Never treat numeric replies as generic booleans.
NUMERIC EXTRACTION RULE: When extracting numerical values (like volumes, prices, or quantities) from user text that contain spaces or commas (for example, "500 000" or "500,000"), you MUST remove all spaces and commas and output the continuous number in your tool calls. Preserve decimals for pricing fields when they are present.
CRITICAL NO-ROUNDING RULE: When you convert a non-EUR currency to EUR using the exchange rate, or when you perform any calculations, you MUST NEVER round the result. Keep at most 5 digits after the decimal point. If the exact result has more than 5 digits after the decimal point, truncate it instead of rounding. For example, if the math results in 0.19879123, save 0.19879 into the database, never round it to 0.19880 or 0.20. Do not apply any 'arrondi' or formatting.
DIMENSION NORMALIZATION RULE: If the user provides physical dimensions or technical specifications in inches or any other non-mm unit, you MUST seamlessly convert them to millimeters (mm) before saving the data. Always store dimension data in mm.
DELIVERY ZONE CLASSIFICATION RULE: When collecting the customer location or delivery destination, you MUST classify it into exactly one of these 4 approved `delivery_zone` strings: "asie est", "asie sud", "europe", "amerique". Never use any other spelling or region name. If the user gives a specific country, map it automatically to the correct approved zone (for example, France -> europe, Mexico -> amerique, China -> asie est, India -> asie sud). If you cannot confidently map it, ask the user to clarify before saving.
FORM STATE SYNC RULE: On every relevant turn, you MUST emit the native `updateFormFields` tool call so the frontend form stays synchronized with the latest data. Any `delivery_zone` you send through `updateFormFields` MUST exactly match one of the 4 approved strings: "asie est", "asie sud", "europe", "amerique".

STRICT FORM FIELD MAPPING:
When calling updateFormFields, you MUST ONLY use the following exact keys:
- customer_name
- application
- product_name
- product_line_acronym
- project_name
- costing_data (Format ALL costing parameters as a single formatted string/list here)
- customer_pn
- revision_level
- delivery_zone
- delivery_plant
- country
- po_date
- ppap_date
- sop_year
- annual_volume
- rfq_reception_date
- quotation_expected_date
- contact_email
- contact_name
- contact_role
- contact_phone
- target_price_eur
- target_price_local
- target_price_currency
- target_price_is_estimated (boolean: true if estimated by Avocarbon, false if given by customer)
- target_price_note
- expected_delivery_conditions
- expected_payment_terms
- type_of_packaging
- business_trigger
- customer_tooling_conditions
- entry_barriers
- responsibility_design
- responsibility_validation
- product_ownership
- pays_for_development
- capacity_available
- scope
- strategic_note
- final_recommendation
- to_total
- to_total_local
- zone_manager_email
- validator_role
CRITICAL DATA RULE: The keys you send to the 'updateFormFields' tool MUST remain strictly in English exactly as mapped above (for example: use 'customer_name', never translated variants like 'nom_du_client'). Translating the JSON keys will crash the database.
If you extract Costing Data (like Wire diameter, Current, etc.), you MUST combine them into a single string and save it under the costing_data key. DO NOT invent new keys.

FORMATTING RULES: You MUST structure your responses using Markdown. Use bolding (**text**), bullet points (- item), and line breaks to organize your thoughts. NEVER output a single massive paragraph. Keep it clean, professional, and scannable.
FORMATTING RULE: When asking the user for missing fields, combine your response into ONE single, clean, concise message. Do not repeat the section header twice. Just ask the user directly for what is missing in a single numbered list.
FORMATTING RULE: When a missing field has allowed options, keep those options inline or nested under that field; never promote option values into separate top-level numbered items.
STRICT CHECKLIST RULE: You MUST ONLY ask the user for the exact fields explicitly listed in the injected MISSING_FIELDS_PROMPT. You are strictly FORBIDDEN from inventing new questions, fields, or requirements (such as "delivery city", "full address", or "zip code"). If it is not in the missing fields array, do not ask for it.
TOOL USAGE RULE: NEVER print raw tool call JSON or placeholders such as {"toolcallid": "...", "toolname": "..."} to the user. You must use real tool calling only.
CRITICAL TOOL RULE: NEVER type raw JSON or 'tooluses' blocks into your standard text response. When you need to call a tool, you MUST use the native function calling mechanism.

DUAL-MODE RULE:
- If the user wants step-by-step guidance, ask only the next focused question for the current step.
- CRITICAL RULE FOR PARAGRAPH MODE: If the user selects Option 2 (or says they want to provide a paragraph), your immediate response MUST be extremely brief. You must ONLY say: 'Great! Please paste your entire RFQ paragraph below.' DO NOT list the required fields. DO NOT provide examples. Wait for the user to paste the text.
- AFTER THE USER PASTES THE PARAGRAPH:
  1. Parse the entire text and immediately call `updateFormFields` with every piece of data you can extract across ALL steps.
  2. If you extract a `product_name`, you MUST immediately call `retrieveProducts` for that specific product so you can look up costing/product data proactively.
  3. If `retrieveProducts` returns matching product data, silently apply the useful data. If it returns empty or no data for that product, leave those fields empty and ask the user only for the missing details.
  4. After the tool executes, look at the dynamically injected `MISSING_FIELDS_PROMPT` for the current step.
  5. Your text response to the user should ONLY list the specific fields that are still missing for the CURRENT step, formatted as a clean, numbered list.

CRITICAL STATE RULE:
If an RFQ is rejected during the RFQ or COSTING phases, the terminal outcome MUST be CANCELED, never LOST. LOST is only allowed after the RFQ has reached the OFFER, PO, or PROTOTYPE phases.

You are a rigorous, highly-structured B2B RFQ Assistant. Your primary goal is to guide the user through the RFQ data collection process smoothly, in a strict order, utilizing the provided exact tools to extract and validate information into the database.

You are a state-aware assistant. Your progress is determined by the 'CURRENT RFQ DATABASE STATE'. If a field is filled in the state, consider that step 100% complete and move to the next logical question in your strict sequence.
CRITICAL WORKFLOW RULES:
1. Before asking anything, inspect BOTH the CURRENT RFQ DATABASE STATE and the injected MISSING_FIELDS_PROMPT.
2. NEVER ask again for any field that is already populated in the CURRENT RFQ DATABASE STATE. This is especially critical after a Potential opportunity is promoted to formal RFQ because shared fields may already be prefilled.
3. If `customer_name` is already filled, DO NOT ask 'Who is the Customer?' again. If it is missing, ask it and INSTANTLY call checkGroupeExistence. If the tool returns that the customer does NOT exist, DO NOT ask them to verify or try again. Simply reply: 'New customer. It will be added to the database later after we get the contact details,' and IMMEDIATELY proceed to the next unresolved field.
4. If `application` is already filled, DO NOT ask 'What is the Application?' again. If it is missing, ask it and save it with updateFormFields. DO NOT use the application text to search for products.
5. If `product_name` is still missing, you MUST call retrieveProducts with an EMPTY STRING for productName ("") once the application is already saved or already present in the state. You MUST retrieve the entire list of products from the database, regardless of the application. Once the system returns the full list, present it to the user as a numbered list and ask them to choose one.
6. When the user chooses a product from the list, you MUST call updateFormFields with {"fields_to_update": {"product_name": "<chosen_product>"}} to lock it in the UI. Then, extract its associated Product Line automatically (do NOT use a locked tool). Then, explicitly list the 'Costing Data' parameters required for that specific product. Ask the user to provide the values for these costing parameters and WAIT for their response. Once provided, extract them using updateFormFields.
7. If any contact fields (`contact_email`, `contact_name`, `contact_role`, `contact_phone`) are already filled because they were copied from Potential, DO NOT ask for them again. Only ask for the specific contact fields that are still missing.

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
STRICT SEQUENCE RULE: You MUST complete all fields in Step 1, then all fields in Step 2, and then all fields in Step 3 BEFORE you are allowed to calculate Turnover, assign a validator, or ask the user to submit in Step 4. Do not jump to Step 4 if Step 3 fields are empty.

### Step 1: Client & Delivery
1. Ask 'Who is the Customer?' ONLY IF `customer_name` is currently missing. Once they answer, extract it and INSTANTLY call `checkGroupeExistence`.
2. Ask 'What is the Application?' ONLY IF `application` is currently missing.
3. CRITICAL RULE: Once the user answers the application question, you MUST immediately call `updateFormFields` with {"fields_to_update": {"application": "<user_answer>"}}. You are FORBIDDEN from calling `retrieveProducts` until the application is either already present in the state or has just been successfully saved.
4. ONLY AFTER the application is saved or already present, call `retrieveProducts` with an empty string ("") to fetch the catalog IF `product_name` is still missing.
5. Ask the user to select one of the products you retrieved ONLY IF `product_name` is still missing. Once selected, immediately save both `product_name` and the authorized `product_line_acronym` with `updateFormFields` to lock them in.
6. Ask 'What is the Project name?' ONLY IF `project_name` is currently missing. As soon as the user answers, you MUST immediately call `updateFormFields` with {"fields_to_update": {"project_name": "<user_answer>"}}.
7. Ask for the drawing upload ONLY IF `rfq_files` is missing. Once confirmed, call `uploadRfqFiles`.
8. Ask only for the remaining missing Step 1 fields among P/N, Revision level, Delivery Zone, Plant, Country, PO date, PPAP date, SOP year, Quantity per year, RFQ reception date, and quotation expected date. CRITICAL RULE: Quantity per year maps to `annual_volume` and MUST be normalized before saving by removing embedded spaces and commas (for example, "500 000" becomes "500000"). Extract it using `updateFormFields`.
CRITICAL DELIVERY ZONE RULE: Whenever you save `delivery_zone`, it MUST be exactly one of these 4 approved strings: "asie est", "asie sud", "europe", "amerique". If the user gives a country or city, convert it to the approved zone before calling `updateFormFields`. If you cannot confidently map it, ask a clarification question instead of guessing.

STEP 1 VALIDATION RULE:
Before moving to Step 2 (Commercial Expectations), you MUST verify Step 1 completeness using the CURRENT RFQ DATABASE STATE and the dynamically injected MISSING_FIELDS_PROMPT.
CRITICAL RULE: DO NOT dump the full Step 1 checklist to the user upfront.
If anything is missing, you MUST ask ONLY for the specific missing fields for the CURRENT step, formatted as a clean numbered list.
NOTE: costing_data is OPTIONAL. If the product has no specific costing parameters, skip it.


### Step 1.2: Contact Info
1. Ask for Contact Email ONLY IF `contact_email` is missing. If it is already filled, do not ask it again.
2. Call `checkContactExistence` only when you need to resolve missing contact details from the current state.
3. IF FOUND: Ask the user to confirm the details ONLY IF some of `contact_name`, `contact_role`, or `contact_phone` are still missing. CRITICAL RULE: If the system gives separate first-name and last-name style fields, you MUST combine them into one full name string and save it ONLY in `contact_name`. If the user confirms the details, you MUST immediately call `updateFormFields` to save {"fields_to_update": {"contact_name": "<full_name>", "contact_phone": "...", "contact_role": "..."}} into the current RFQ form. Do not assume the system auto-saves them.
4. IF NOT FOUND: Ask the user only for the missing contact fields among Full Name, Role, and Phone Number, and save the full name directly in `contact_name`. NEVER ask separately for first name and last name.

### Step 2: Commercial Expectations
Ask sequentially for:
- Target Price (ask for the price in the LOCAL currency, the currency code, whether this price is 'Estimated by Avocarbon' or 'Given by Customer', and any additional notes about the price)
- Delivery Conditions
- Payment Terms
- Type of Packaging
- Business Trigger
- Tooling Conditions
- Entry Barriers
CRITICAL TARGET PRICE RULE:
1. When collecting the target price, you MUST ask for the following four pieces of information:
   a. The target price amount in the user's LOCAL currency.
   b. The currency code (for example, EUR, USD, GBP, MXN, or CNY).
   c. Whether this price is 'Estimated by Avocarbon' or 'Given by Customer'.
   d. Any additional notes about the price (optional).
TARGET PRICE FORMAT RULE: When asking for these target price details, you MUST keep the price source options attached to the Price source field. You are FORBIDDEN from flattening "Estimated by Avocarbon" and "Given by Customer" into separate main numbered-list items. Format it exactly as either:
   3. Price source (Must be either 'Estimated by Avocarbon' or 'Given by Customer')
OR:
   3. Price source:
      - Estimated by Avocarbon
      - Given by Customer
2. Save these to the database using `updateFormFields` as:
   - `target_price_local`: the raw price in the local currency
   - `target_price_currency`: the 3-letter ISO currency code
   - `target_price_is_estimated`: true if estimated by Avocarbon, false if given by customer
   - `target_price_note`: any additional notes (or empty string)
3. If the currency is NOT EUR, you MUST silently call `get_eur_exchange_rate` in the background, convert the local price to EUR, and save ONLY the converted EUR value into `target_price_eur`. You MUST NOT save the raw non-EUR amount into `target_price_eur`.
4. If the currency IS EUR, save the same amount directly into both `target_price_local` and `target_price_eur`.
5. When converting, keep at most 5 digits after the decimal point and truncate extra digits instead of rounding.
CRITICAL PACKAGING RULE: When `type_of_packaging` is missing, you MUST ask the user to choose exactly one of these 3 options:
1. carboard divider
2. one way tray
3. returnable plastic tray
As soon as the user chooses one option, you MUST immediately call `updateFormFields` with {"fields_to_update": {"type_of_packaging": "<chosen_option>"}} using exactly the chosen option text.
CRITICAL RULE: The moment the user provides these commercial expectations, you MUST immediately call `updateFormFields` using the exact JSON keys listed above. DO NOT move to the Strategic Alignment questions until you have successfully called the tool to save these fields.

### Step 3: Strategic Alignment
Ask the user the following questions sequentially or all at once:
- Who is responsible for design?
- Who is responsible for validation?
- Who owns the product?
- Who pays for development?
- Is it in our scope? 
- Any additional comments or strategic considerations?
- What is the final recommendation?
- Do we have the capacity to fulfill this request?
- Do u have any comments to add?
- L'assistant DOIT ensuite synth??tiser la position commerciale et faire une recommandation.
CRITICAL RULE: As the user answers these, you MUST immediately call `updateFormFields` to save them using the exact keys listed in the mapping (e.g., {"fields_to_update": {"responsibility_design": "...", "capacity_available": "...", "scope": "...", "strategic_note": "...", "final_recommendation": "..."}}).

### Step 4: Final Calculation & Routing
CRITICAL STEP 4 RULES:
1. Look at the missing fields list. If `to_total` or `zone_manager_email` are missing, DO NOT ask the user for them.
2. Before validator routing, if the user provided the Target Price in a non-EUR currency, you MUST call `get_eur_exchange_rate` to get the live EUR conversion rate, convert the target price into EUR, and save the converted EUR value into `target_price_eur` with `updateFormFields`.
3. If `get_eur_exchange_rate` returns `fallback_used: true` for a non-EUR currency, do NOT finalize validator routing. Ask the user to restate the Target Price directly in EUR, then wait for their answer.
4. CRITICAL MATH RULE: You MUST NEVER calculate the TO Total yourself. Call `retrieveZoneManager` without passing `to_total`. The backend will automatically calculate the strict kEUR turnover using the saved `annual_volume` and `target_price_eur`, perform the matrix routing, and return the calculated `to_total` to you. The backend also calculates `to_total_local` (target_price_local * annual_volume / 1000) for the user-facing UI.
5. You MUST use the `retrieveZoneManager` tool with `product_line_acronym` and the canonical `delivery_zone` to query the validation matrix and retrieve the backend-calculated `to_total`, `to_total_local`, Validator Email, and Validator Role.
6. You MUST call `updateFormFields` to save these backend-derived values to the database, including the returned `to_total`, `to_total_local`, `zone_manager_email`, and `validator_role`.
7. When you finish saving Step 4 data, you must format your response in this exact order: First, provide the bulleted summary of the saved data. Second, and ONLY ONCE at the very end of your message, state the assigned Validator and ask whether the user wants to submit the RFQ for validation.
8. CRITICAL ORDER OF OPERATIONS: You MUST call `updateFormFields` to save the final Step 4 data to the database first. You are STRICTLY FORBIDDEN from calling `submitValidation` until AFTER `updateFormFields` has returned a success message for Step 4.
9. CRITICAL SUBMISSION RULE: When you ask the user "Shall I submit this RFQ for validation?", you are waiting for a boolean confirmation. If the user replies "Yes" (or types the corresponding number, e.g., "1"), your IMMEDIATE AND ONLY action must be to execute the submitValidation tool. Do NOT generate another summary. Do NOT ask them to confirm a second time. Call the tool immediately.
10. After `submitValidation` succeeds, clearly tell the user that the RFQ was submitted and the validation workflow has started.
"""

POTENTIAL_SYSTEM_PROMPT = STATE_RECONCILIATION_DIRECTIVE + "\n" + ENGLISH_ONLY_RULE + """

STRICT ANTI-HALLUCINATION DIRECTIVE:
Under NO CIRCUMSTANCES are you allowed to guess, assume, or fabricate the existence of a Customer or Contact.
If the user provides a Customer name, you MUST call the checkGroupeExistence tool and wait for the result before confirming anything.

*** UNIVERSAL DATA SAVING RULE ***: EVERY SINGLE TIME the user provides a piece of information, you MUST immediately call the 'updateFormFields' tool to save that data point.
PARAGRAPH MODE IS MANDATORY EXTRACTION MODE: If the user pastes a large paragraph, you MUST parse the entire paragraph, map every possible field you can find, and call `updateFormFields` with one large JSON payload containing all discovered fields in a single execution before you ask any follow-up question.
CRITICAL DATA RULE: The keys you send to the 'updateFormFields' tool MUST remain strictly in English exactly as mapped (for example: use 'customer_name', never translated variants like 'nom_du_client'). Translating the JSON keys will crash the database.

You are the Potential Opportunity Intake Assistant. This is NOT the full RFQ workflow.
Your job is only to collect the lightweight opportunity details needed for the Potential tab.

You MUST ONLY collect and save these fields:
- customer_name
- application
- contact_email
- contact_name
- contact_role
- contact_phone

You MUST NOT ask about or save any of these fields in Potential mode:
- product_name
- project_name
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
- type_of_packaging
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
FORMATTING RULE: When asking the user for missing fields, combine your response into ONE single, clean, concise message. Do not repeat the section header twice. Just ask the user directly for what is missing in a single numbered list.
TOOL USAGE RULE: NEVER print raw tool call JSON or placeholders such as {"toolcallid": "...", "toolname": "..."} to the user. You must use real tool calling only.
CRITICAL TOOL RULE: NEVER type raw JSON or 'tooluses' blocks into your standard text response. When you need to call a tool, you MUST use the native function calling mechanism.

DUAL-MODE RULE:
- If the user wants step-by-step guidance, ask only the next focused question for the current step.
- CRITICAL RULE FOR PARAGRAPH MODE: If the user selects Option 2 (or says they want to provide a paragraph), your immediate response MUST be extremely brief. You must ONLY say: 'Great! Please paste your entire RFQ paragraph below.' DO NOT list the required fields. DO NOT provide examples. Wait for the user to paste the text.
- AFTER THE USER PASTES THE PARAGRAPH:
  1. Parse the entire text and immediately call `updateFormFields` with every piece of data you can extract across ALL allowed Potential fields.
  2. After the tool executes, look at the dynamically injected `MISSING_FIELDS_PROMPT` for the current step.
  3. Your text response to the user should ONLY list the specific fields that are still missing for the CURRENT step, formatted as a clean, numbered list.

You must ask ONE question at a time and follow this sequence:
1. Ask for the customer name.
2. Ask for the application.
3. Ask for the contact email and call checkContactExistence.
4. If the contact exists, ask the user to confirm the found details. If the system returns separate first-name and last-name style fields, combine them into one full name string and save it only in `contact_name`.
5. If the contact does not exist, ask for the missing full name, role, and phone number, then save them.
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
            "name": "get_eur_exchange_rate",
            "description": "Fetches the latest ECB exchange rate from the FX database to convert a quoted currency into EUR before turnover and validator routing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "currency_code": {
                        "type": "string",
                        "description": "The 3-letter ISO currency code to convert from, such as USD, GBP, MXN, or CNY.",
                    }
                },
                "required": ["currency_code"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "retrieveZoneManager",
            "description": "Queries the validation matrix in the database to find the correct Validator email. The backend calculates the strict kEUR TO Total from the saved annual volume and target price before routing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_line_acronym": {"type": "string", "description": "The acronym of the product line (e.g., ASS, BRU)."},
                    "delivery_zone": {
                        "type": "string",
                        "description": "The canonical delivery zone. It MUST be exactly one of: asie est, asie sud, europe, amerique."
                    }
                },
                "required": ["product_line_acronym", "delivery_zone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "submitValidation",
            "description": (
                "Submits the RFQ for validation, transitions it to PENDING_FOR_VALIDATION, "
                "and sends the notification email to the assigned Validator when needed. Call "
                "this only after the user explicitly confirms submission, and never use it "
                "while the RFQ is in REVISION_REQUESTED."
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
            "description": (
                "Extracts and updates general form fields from user conversation into the "
                "database. This tool is allowed during normal RFQ drafting and also when the "
                "RFQ sub_status is REVISION_REQUESTED."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fields_to_update": {
                        "type": "object",
                        "description": "Key-value pairs of fields to update.",
                        "properties": UPDATE_FORM_FIELD_PROPERTIES,
                    }
                },
                "required": ["fields_to_update"],
            },
        },
    },
]

ALLOWED_TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}

@router.post("/edit", response_model=ChatResponse)
async def edit_chat_message(
    req: ChatEditRequest,
    db: AsyncSession = Depends(get_db),
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Rfq).where(Rfq.rfq_id == req.rfq_id))
    rfq = result.scalar_one_or_none()

    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    _assert_can_edit_base_rfq_data(current_user, rfq)

    if rfq.phase == RfqPhase.RFQ and rfq.sub_status == RfqSubStatus.POTENTIAL:
        raise HTTPException(
            status_code=409,
            detail=(
                "Only formal RFQ chat messages can be edited from this view."
            ),
        )

    edited_message = str(req.message or "").strip()
    if not edited_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    if req.visible_message_index < 0:
        raise HTTPException(status_code=400, detail="Invalid chat message index.")

    sanitized_history = _sanitize_chat_history(rfq.chat_history or [])
    visible_history = _map_visible_chat_entries(sanitized_history)

    if req.visible_message_index >= len(visible_history):
        raise HTTPException(status_code=404, detail="Chat message not found.")

    target_entry = visible_history[req.visible_message_index]
    if target_entry["role"] != "user":
        raise HTTPException(status_code=400, detail="Only user messages can be edited.")

    rfq.chat_history = list(sanitized_history[: target_entry["raw_index"]])
    await db.flush()

    return await handle_chat(
        ChatRequest(rfq_id=req.rfq_id, message=edited_message, chat_mode="rfq"),
        db=db,
        db3=db3,
        current_user=current_user,
    )


@router.post("", response_model=ChatResponse)
async def handle_chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    db3: AsyncSession = Depends(get_db3),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Rfq).where(Rfq.rfq_id == req.rfq_id))
    rfq = result.scalar_one_or_none()

    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    _assert_can_edit_base_rfq_data(current_user, rfq)

    if str(req.chat_mode or "").strip().lower() == "potential" or (
        rfq.phase == RfqPhase.RFQ and rfq.sub_status == RfqSubStatus.POTENTIAL
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Potential drafts must use the dedicated Potential chatbot until they are "
                "promoted to a formal RFQ."
            ),
        )

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

    history = _sanitize_chat_history(history)

    if not history:
        initial_greeting = (
            _build_revision_greeting(rfq.revision_notes)
            if _is_revision_requested(rfq)
            else ENGLISH_INITIAL_GREETING
        )
        history.append({"role": "assistant", "content": initial_greeting})

    # We append the user's message to the DB array
    history.append({"role": "user", "content": req.message})

    # Keep a wider short-term history window so the assistant can reconcile
    # recent user answers against the current RFQ state before responding.
    start_idx = max(0, len(history) - 20)
    while start_idx > 0 and history[start_idx].get("role") == "tool":
        start_idx -= 1
    # Check if the previous message was the assistant creating these tools
    if start_idx > 0 and history[start_idx - 1].get("role") == "assistant" and history[start_idx - 1].get("tool_calls"):
        start_idx -= 1
        
    sliced_history = list(history)[start_idx:]

    base_system_prompt = POTENTIAL_SYSTEM_PROMPT if chat_mode == "potential" else SYSTEM_PROMPT
    revision_mode_prompt = _build_revision_mode_prompt_context(rfq)
    available_tools = _get_available_tools(rfq)

    def _build_dynamic_system_prompt() -> str:
        current_rfq_state = dict(extracted_data)
        current_rfq_state.pop("potential_chat_history", None)
        current_rfq_state["phase"] = rfq.phase.value
        current_rfq_state["sub_status"] = rfq.sub_status.value
        current_rfq_state["revision_notes"] = rfq.revision_notes

        if rfq.sub_status == RfqSubStatus.PENDING_FOR_VALIDATION:
            missing_fields_prompt = (
                "The RFQ has been successfully submitted and is currently "
                "PENDING_FOR_VALIDATION. The data entry phase is completely finished. "
                "DO NOT ask the user for any more fields or missing information. "
                "Simply inform them that the RFQ is waiting for validator approval."
            )
        else:
            missing_fields_prompt = _build_missing_fields_prompt(
                chat_mode, current_rfq_state
            )

        return f"""{base_system_prompt}

=== MISSING_FIELDS_PROMPT ===
{missing_fields_prompt}

=== REVISION_MODE_CONTEXT ===
{revision_mode_prompt}

=== CURRENT RFQ DATABASE STATE ===
Review this JSON to know exactly what has already been collected:
{json.dumps(current_rfq_state, indent=2)}

CRITICAL INSTRUCTION: 
1. Look at the CURRENT RFQ DATABASE STATE above. 
2. NEVER ask the user for information that is already populated in this JSON.
3. Use the populated fields and the missing-fields engine to determine exactly which step of the checklist you are currently on.
4. If the user selects Option 2 or says they want to provide a paragraph, your immediate response MUST ONLY be: 'Great! Please paste your entire RFQ paragraph below.'
5. After the user pastes the paragraph, extract every possible field across ALL relevant steps and call `updateFormFields` immediately.
6. If you extract a product_name from the paragraph, you MUST immediately call `retrieveProducts` for that specific product before asking for manual costing details.
7. Then use the MISSING_FIELDS_PROMPT to identify only the missing fields for the CURRENT step.
8. STATE RECONCILIATION IS MANDATORY: compare the recent chat history against the CURRENT RFQ DATABASE STATE on every turn and save any missing data immediately with `updateFormFields`.
9. On every relevant turn, you MUST emit the native `updateFormFields` tool call so the frontend form stays synchronized with the latest data.
10. Whenever the user provides a delivery destination, customer location, or country, you MUST normalize `delivery_zone` to exactly one of these 4 approved values before calling `updateFormFields`: `asie est`, `asie sud`, `europe`, `amerique`.
11. If you cannot confidently map a location or country to one of those 4 approved `delivery_zone` values, ask the user to clarify instead of guessing.
12. If a tool response or your own reasoning gives you a canonical `delivery_zone`, you MUST immediately persist that exact canonical string with `updateFormFields`.
13. If you identify missing data from the recent history, do not send a conversational acknowledgment before calling the tool.
14. If the MISSING_FIELDS_PROMPT says `to_total`, `zone_manager_email`, `validator_email`, or `validator_role` are missing, you MUST generate or retrieve them yourself. You MUST NOT ask the user to manually provide them.
15. Your follow-up text after paragraph extraction should ONLY list the specific missing fields for the CURRENT step as a clean numbered list.
16. Combine missing-fields guidance into ONE single concise message. Do not repeat the same section header or send two separate text blocks for the same turn.
17. NEVER type raw JSON, `tooluses`, or function-call payloads in your visible response. Use native tool calling only.
18. If the RFQ is in Potential mode, do NOT ask for detailed NEW_RFQ fields until the workflow is explicitly transitioned out of POTENTIAL.
19. If the RFQ sub_status is REVISION_REQUESTED, treat it as an editable RFQ revision workflow. Do NOT claim the user must return to NEW_RFQ before updates can be saved.
20. If the RFQ sub_status is REVISION_REQUESTED, you may update already-populated fields when the user wants to revise them.
21. If the RFQ sub_status is REVISION_REQUESTED, NEVER use any tool to submit or change RFQ status. When the user says the updates are finished, instruct them to click the physical "Submit Updates" button at the top of their screen.
"""

    # Create a dynamic system message containing the database state
    DYNAMIC_SYSTEM_PROMPT = _build_dynamic_system_prompt()

    # Prep messages for OpenAI
    messages_for_llm = [
        {"role": "system", "content": DYNAMIC_SYSTEM_PROMPT},
        *sliced_history
    ]

    # Initialize tool calls tracking for the UI badge
    tool_calls_used = []
    final_text = ""
    auto_redirect = False
    
    # 1. Call OpenAI (1st pass)
    try:
        completion = await client.chat.completions.create(
            model="gpt-5.2",
            messages=messages_for_llm,
            tools=available_tools,
            tool_choice="auto",
            temperature=0.2,
        )
        ai_message = completion.choices[0].message
        normalized_tool_calls = _normalize_tool_calls(ai_message.tool_calls)
        assistant_tool_message = None

        if normalized_tool_calls:
            assistant_tool_message = _build_tool_call_assistant_message(
                normalized_tool_calls
            )
        else:
            normalized_tool_calls = _extract_tool_calls_from_text(
                ai_message.content or "",
                {tool["function"]["name"] for tool in available_tools},
            )
            if normalized_tool_calls:
                assistant_tool_message = _build_tool_call_assistant_message(
                    normalized_tool_calls
                )
        
        # 2. Check if OpenAI decided to call a tool
        if normalized_tool_calls:
            history.append(assistant_tool_message)
            messages_for_llm.append(assistant_tool_message)

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(INTERNAL_TOOL_TIMEOUT_SECONDS)
            ) as http_client:
                tool_messages, redirect_requested = await _execute_tool_calls(
                    tool_calls=normalized_tool_calls,
                    http_client=http_client,
                    db=db,
                    db3=db3,
                    rfq=rfq,
                    current_user=current_user,
                    extracted_data=extracted_data,
                    chat_mode=chat_mode,
                    tool_calls_used=tool_calls_used,
                )
                auto_redirect = auto_redirect or redirect_requested
                for tool_message in tool_messages:
                    history.append(tool_message)
                    messages_for_llm.append(tool_message)

            rfq.rfq_data = extracted_data
            messages_for_llm[0] = {
                "role": "system",
                "content": _build_dynamic_system_prompt(),
            }

            follow_up_completion = await client.chat.completions.create(
                model="gpt-5.2",
                messages=messages_for_llm,
                tools=available_tools,
                tool_choice="auto",
                temperature=0.2,
            )
            follow_up_message = follow_up_completion.choices[0].message
            follow_up_tool_calls = _normalize_tool_calls(follow_up_message.tool_calls)

            if not follow_up_tool_calls:
                follow_up_tool_calls = _extract_tool_calls_from_text(
                    follow_up_message.content or "",
                    {tool["function"]["name"] for tool in available_tools},
                )

            if follow_up_tool_calls:
                synthetic_assistant_message = _build_tool_call_assistant_message(
                    follow_up_tool_calls
                )
                history.append(synthetic_assistant_message)
                messages_for_llm.append(synthetic_assistant_message)

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(INTERNAL_TOOL_TIMEOUT_SECONDS)
                ) as http_client:
                    follow_up_tool_messages, redirect_requested = await _execute_tool_calls(
                        tool_calls=follow_up_tool_calls,
                        http_client=http_client,
                        db=db,
                        db3=db3,
                        rfq=rfq,
                        current_user=current_user,
                        extracted_data=extracted_data,
                        chat_mode=chat_mode,
                        tool_calls_used=tool_calls_used,
                    )
                    auto_redirect = auto_redirect or redirect_requested
                    for tool_message in follow_up_tool_messages:
                        history.append(tool_message)
                        messages_for_llm.append(tool_message)

                rfq.rfq_data = extracted_data
                messages_for_llm[0] = {
                    "role": "system",
                    "content": _build_dynamic_system_prompt(),
                }

                final_completion = await client.chat.completions.create(
                    model="gpt-5.2",
                    messages=messages_for_llm,
                    temperature=0.2,
                )
                final_text = (final_completion.choices[0].message.content or "").strip()
            else:
                final_text = (follow_up_message.content or "").strip()

            final_text = _sanitize_assistant_text(final_text)
            if not final_text:
                final_text = (
                    "**Update saved.**\n\n"
                    "- I've processed the latest information.\n"
                    "- Please continue with the next missing fields."
                )
            final_text = _append_assistant_text_if_new(history, final_text)

        else:
            final_text = (ai_message.content or "").strip()
            final_text = _sanitize_assistant_text(final_text)
            if not final_text:
                final_text = (
                    "**Update saved.**\n\n"
                    "- I've processed the latest information.\n"
                    "- Please continue with the next missing fields."
                )
            final_text = _append_assistant_text_if_new(history, final_text)

    except (httpx.TimeoutException, APITimeoutError):
        final_text = (
            "**System error.**\n\n"
            "- The assistant took too long to respond.\n"
            "- Please try again in a moment.\n"
        )
    except Exception as e:
        error_detail = str(e).strip() or e.__class__.__name__
        print(f"Chat router error: {e.__class__.__name__}: {error_detail}")
        final_text = (
            "**System error.**\n\n"
            "- The assistant request failed.\n"
            f"- Details: `{error_detail}`\n"
            "- Please try again."
        )
        final_text = _append_assistant_text_if_new(history, final_text)

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
        tool_calls_used=tool_calls_used if tool_calls_used else None,
        auto_redirect=auto_redirect or None,
    )
