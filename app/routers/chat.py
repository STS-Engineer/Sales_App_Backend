import json
import logging
import datetime
import re
import unicodedata
from typing import Any
import httpx
from openai import APITimeoutError, AsyncOpenAI
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, get_db3
from app.middleware.auth import get_current_user
from app.models.rfq import Rfq, RfqDocumentType, RfqPhase, RfqSubStatus
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
from app.schemas.rfq import (
    get_incomplete_product_fields,
    normalize_rfq_data_products,
)
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
ENGLISH_INITIAL_GREETING_TEMPLATE = (
    "Hello, I'm your sales assistant. I'll be helping you fill your RFQ. "
    "How would you like to proceed?\n"
    "1. Guide me step by step\n"
    "2. I will provide a whole paragraph"
)
RFQ_PARAGRAPH_MODE_PROMPT = "Great! Please paste your entire RFQ paragraph below."
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
    "automotive_type",
    "product_name",
    "product_line_acronym",
    "project_name",
    "costing_data",
    "products",
    "volumes",
    "total_target_to",
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
    "automotiveType": "automotive_type",
    "productName": "product_name",
    "products": "products",
    "productItems": "products",
    "lineItems": "products",
    "volumes": "volumes",
    "totalTargetTo": "total_target_to",
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
PRODUCT_ITEM_TOOL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "product": {"type": "string"},
            "application": {"type": "string"},
            "part_number": {"type": "string"},
            "product_line": {"type": "string"},
            "costing_data": {"type": "string"},
            "po_date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format.",
            },
            "ppap_date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format.",
            },
            "sop": {
                "type": "number",
                "description": "SOP year for this product row.",
            },
            "revision_level": {"type": "string"},
            "quantity": {"type": "number"},
            "target_price": {
                "type": "number",
                "description": (
                    "The exact raw local line price stated by the user. "
                    "Never convert currencies yourself."
                ),
            },
            "currency": {
                "type": "string",
                "description": (
                    "The exact 3-letter currency code provided by the user "
                    "for this product row."
                ),
            },
            "target_price_is_estimated": {
                "type": "boolean",
                "description": (
                    "true for Estimated by Avocarbon, false for Official "
                    "Customer Price."
                ),
            },
            "target_to": {
                "type": "number",
                "description": (
                    "Derived turnover only. Do not invent it and do not "
                    "currency-convert it yourself."
                ),
            },
        },
    },
}
VOLUME_ITEM_TOOL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "target_price": {"type": "number"},
            "price_source": {
                "type": "string",
                "description": (
                    "Use the exact text supplied by the user, typically "
                    "'Estimated' or 'Official Customer Price'."
                ),
            },
            "delivery_zone": {
                "type": "string",
                "description": (
                    "Must be one of the 7 approved delivery zones when known."
                ),
            },
            "plant": {"type": "string"},
            "country": {"type": "string"},
            "volumes": {
                "type": "object",
                "description": (
                    "Year-to-quantity mapping such as {'2027': 120000, "
                    "'2028': 130000}."
                ),
                "additionalProperties": {"type": "number"},
            },
        },
    },
}
UPDATE_FORM_FIELD_PROPERTIES["products"] = PRODUCT_ITEM_TOOL_SCHEMA
UPDATE_FORM_FIELD_PROPERTIES["volumes"] = VOLUME_ITEM_TOOL_SCHEMA
UPDATE_FORM_FIELD_PROPERTIES["total_target_to"] = {"type": "number"}
UPDATE_FORM_FIELD_PROPERTIES["to_total"] = {"type": "number"}
UPDATE_FORM_FIELD_PROPERTIES["to_total_local"] = {"type": "number"}
UPDATE_FORM_FIELD_PROPERTIES["target_price_is_estimated"] = {"type": "boolean"}
AI_GENERATED_STEP_FIELDS = [
    "total_target_to",
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


def _step_field(name: str, *, is_optional: bool = False) -> dict[str, object]:
    return {"name": name, "is_optional": is_optional}


RFQ_STEPS: list[tuple[int, list[dict[str, object]]]] = [
    (
        1,
        [
            _step_field("automotive_type"),           # Customer Details 1
            _step_field("customer_name"),             # Customer Details 2
            _step_field("project_name"),              # Customer Details 3 (formulaire: 3e champ visible)
            _step_field("product_name"),              # Products table col 1
            _step_field("product_line_acronym"),      # Products table col 2
            _step_field("costing_data", is_optional=True),  # Products table col 3 (optional)
            _step_field("application"),               # Products table col 4
            _step_field("products"),                  # Products section (col 5: part_number first)
            _step_field("rfq_files"),                 # Products table col 6 (Drawing/Files)
            _step_field("sop_year"),                  # Products table col 7 (SOP Year)
            _step_field("delivery_zone"),             # Volumes table
            _step_field("delivery_plant"),            # Volumes table
            _step_field("country"),                   # Volumes table
            _step_field("po_date"),                   # Logistics
            _step_field("ppap_date", is_optional=True),  # Logistics (optional)
            _step_field("rfq_reception_date"),        # Logistics
            _step_field("quotation_expected_date"),   # Logistics
            _step_field("contact_name"),              # Contact
            _step_field("contact_role"),              # Contact
            _step_field("contact_phone"),             # Contact
            _step_field("contact_email"),             # Contact
        ],
    ),
    (
        2,
        [
            _step_field("expected_delivery_conditions"),
            _step_field("expected_payment_terms"),
            _step_field("type_of_packaging", is_optional=True),
            _step_field("business_trigger", is_optional=True),
            _step_field("customer_tooling_conditions", is_optional=True),
            _step_field("entry_barriers", is_optional=True),
        ],
    ),
    (
        3,
        [
            _step_field("responsibility_design"),
            _step_field("responsibility_validation"),
            _step_field("product_ownership"),
            _step_field("pays_for_development"),
            _step_field("capacity_available"),
            _step_field("scope"),
            _step_field("strategic_note"),
            _step_field("final_recommendation"),
        ],
    ),
    (
        4,
        [
            _step_field("total_target_to"),
            _step_field("to_total"),
            _step_field("zone_manager_email"),
            _step_field("validator_role"),
        ],
    ),
]

RFQ_OPTIONAL_FIELDS = {
    str(step_field.get("name") or "").strip()
    for _, step_fields in RFQ_STEPS
    for step_field in step_fields
    if bool(step_field.get("is_optional"))
} | {"volumes"}
RFQ_OPTIONAL_PRODUCT_FIELDS = {"revision_level", "costing_data"}
RFQ_CONTACT_FIELDS = {
    "contact_email",
    "contact_name",
    "contact_role",
    "contact_phone",
}
INTERNAL_CUSTOMER_CONTACT_EMAIL_DOMAINS = {"avocarbon.com"}
SKIP_LIKE_TEXT_VALUES = {
    "_",
    "skip",
    "none",
    "n/a",
    "na",
    "notapplicable",
}
URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()]+")
FIELD_LABELS = {
    "automotive_type": "Automotive / Non automotive",
    "customer_name": "Customer",
    "application": "Application",
    "product_name": "Product name",
    "product_line_acronym": "Product line",
    "project_name": "Project name",
    "costing_data": "Costing data",
    "products": "Products",
    "volumes": "Volumes",
    "rfq_files": "RFQ Files",
    "delivery_zone": "Delivery zone",
    "delivery_plant": "Plant",
    "country": "Country",
    "po_date": "PO date",
    "ppap_date": "PPAP date",
    "sop_year": "SOP year",
    "rfq_reception_date": "RFQ reception date",
    "quotation_expected_date": "Expected quotation date",
    "contact_email": "Contact email",
    "contact_name": "Contact name",
    "contact_role": "Contact function",
    "contact_phone": "Contact phone",
    "expected_delivery_conditions": "Expected Delivery Conditions",
    "expected_payment_terms": "Expected Payment Terms",
    "type_of_packaging": "Type of Packaging",
    "business_trigger": "Business Trigger",
    "customer_tooling_conditions": "Customer Tooling Conditions",
    "entry_barriers": "Entry Barriers",
    "responsibility_design": "Design responsible",
    "responsibility_validation": "Validation responsible",
    "product_ownership": "Design owner",
    "pays_for_development": "Development costs",
    "capacity_available": "Technical capacity",
    "scope": "Scope",
    "strategic_note": "Strategic note",
    "final_recommendation": "Final recommendation",
    "total_target_to": "Total target TO",
    "to_total": "Total turnover",
    "to_total_local": "Total turnover (local)",
    "zone_manager_email": "Validator Email",
    "validator_role": "Validator role",
    "product": "Product",
    "part_number": "Part Number",
    "product_line": "Product line",
    "revision_level": "Revision level",
    "sop": "SOP year",
    "quantity": "Quantity",
    "target_price": "Target Price",
    "currency": "Currency",
    "target_price_is_estimated": "Price source",
    "price_source": "Price source",
}

FIELD_QUESTION_OVERRIDES = {
    "automotive_type": "Is this request related to the Automotive or Non Automotive market?",
    "customer_name": "Who is the Customer?",
    "application": "What is the Application?",
    "product_name": "Which Product name should we use for this RFQ?",
    "project_name": "What is the Project name?",
    "rfq_files": "Have you uploaded the RFQ files (drawings/specs) here?",
    "volumes": "Please provide the yearly volumes and logistics details for each part number.",
    "delivery_zone": "Which delivery zone applies to this RFQ?",
    "delivery_plant": "What is the Plant?",
    "country": "What is the Country?",
    "po_date": "What is the PO date?",
    "ppap_date": "What is the PPAP date?",
    "sop_year": "What is the SOP year?",
    "rfq_reception_date": "What is the RFQ reception date?",
    "quotation_expected_date": "What is the Expected quotation date?",
    "contact_email": "What is the Contact email?",
    "contact_name": "What is the Contact name?",
    "contact_role": "What is the Contact function?",
    "contact_phone": "What is the Contact phone number?",
    "expected_delivery_conditions": "What are the expected Delivery Conditions?",
    "expected_payment_terms": "What are the expected Payment Terms?",
    "type_of_packaging": "Which type of packaging applies?\n\n1. carboard divider\n2. one way tray\n3. returnable plastic tray",
    "business_trigger": "What is the Business Trigger?",
    "customer_tooling_conditions": "What are the Customer Tooling Conditions?",
    "entry_barriers": "What are the Entry Barriers?",
    "responsibility_design": "Who is responsible for design?",
    "responsibility_validation": "Who is responsible for validation?",
    "product_ownership": "Who owns the product?",
    "pays_for_development": "Who pays for development?",
    "capacity_available": "Do we have the capacity to fulfill this request?",
    "scope": "Is it in our scope?",
    "strategic_note": "Do you have any additional comments or strategic considerations?",
    "final_recommendation": "What is the final recommendation?",
    "part_number": "What is the Part Number?",
    "revision_level": "What is the Revision Level? *(Optional — you can omit it.)*",
    "quantity": "What is the Quantity?",
    "target_price": "What is the Target Price?",
    "currency": "What is the Currency?",
    "target_price_is_estimated": "What is the Price source?",
}

SYSTEM_MANAGED_CHAT_FIELDS = {"product_line_acronym"}
PRE_SUBMISSION_MODIFY_PROMPT = (
    "Would you like to update or modify any field before submission?\n\n"
    "Yes\n"
    "No"
)
TYPE_OF_PACKAGING_OPTIONS = [
    "carboard divider",
    "one way tray",
    "returnable plastic tray",
]
AUTOMOTIVE_TYPE_OPTIONS = [
    "1. Automotive",
    "2. Non automotive",
]
PRICE_SOURCE_OPTIONS = [
    "Estimated by Avocarbon",
    "Given by Customer",
]
# Regex that matches internal tool function names — these must never appear in visible text.
_INTERNAL_TOOL_NAME_RE = re.compile(
    r"\b("
    r"updateFormFields|submitValidation|retrieveZoneManager|retrieveProducts"
    r"|checkGroupeExistence|checkContactExistence|get_eur_exchange_rate|uploadRfqFiles"
    r")\b"
)
_PRODUCTS_VOLUME_FIELD_RE = re.compile(
    r"^products\[\d+\]\.(quantity|target_price|currency|target_price_is_estimated)$"
)

# Matches any variant of the submit-for-validation question the LLM may generate.
_SUBMIT_QUESTION_RE = re.compile(
    r"\b(?:do you want|would you like)\s+to\s+submit\s+this\s+\w+\s+for\s+validation\b",
    re.IGNORECASE,
)

# Matches any variant of the pre-submission modify/update prompt the LLM may generate.
_MODIFY_QUESTION_RE = re.compile(
    r"\b(?:"
    r"would you like to (?:update|modify)"
    r"|anything else you (?:want|would like) to (?:update|modify|change)"
    r")\b",
    re.IGNORECASE,
)

# Regex for common scratchpad-reasoning phrases the LLM sometimes leaks.
_SCRATCHPAD_PHRASE_RE = re.compile(
    r"(?:backend-derived|Step\s*\d+\s+says|We(?:'ll| will)\s+call\s+\w+"
    r"|need\s+updateFormFields|should\s+sync|already\s+present\s+but\s+still)",
    re.IGNORECASE,
)


def _is_scratchpad_reasoning_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _INTERNAL_TOOL_NAME_RE.search(stripped):
        return True
    if _SCRATCHPAD_PHRASE_RE.search(stripped):
        return True
    return False


def _strip_scratchpad_reasoning(content: str) -> str:
    """Remove lines that contain leaked LLM internal reasoning or tool names."""
    lines = str(content or "").splitlines()
    filtered = [line for line in lines if not _is_scratchpad_reasoning_line(line)]
    return "\n".join(filtered).strip()


INTERNAL_STATUS_FILLER_SENTENCES = {
    "update saved.",
    "update saved",
    "i've processed the latest information.",
    "i've processed the latest information",
    "please continue with the next missing fields.",
    "please continue with the next missing fields",
    "i've saved the latest potential details.",
    "i've saved the latest potential details",
    "please continue with the next missing information.",
    "please continue with the next missing information",
    "i've saved the latest potential details. please continue with the next missing information.",
    "i've processed the latest information. please continue with the next missing fields.",
}


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


def _normalize_automotive_type(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None

    normalized = re.sub(r"[_\-\s]+", " ", cleaned).strip().lower()
    if normalized == "1":
        return "Automotive"
    if normalized == "2":
        return "Non automotive"
    if "non" in normalized and "auto" in normalized:
        return "Non automotive"
    if "auto" in normalized:
        return "Automotive"
    return cleaned


def _normalize_rfq_data_fields(data: dict | None) -> dict:
    normalized = dict(data or {})
    legacy_scope = normalized.pop("is_feasible", None)
    if "scope" not in normalized and legacy_scope is not None:
        normalized["scope"] = _normalize_scope_value(legacy_scope)
    normalized_automotive_type = _normalize_automotive_type(
        normalized.get("automotive_type") or normalized.get("automotiveType")
    )
    if normalized_automotive_type is not None:
        normalized["automotive_type"] = normalized_automotive_type
    return normalize_rfq_data_products(normalized)


def _get_step_field_name(step_field: dict[str, object] | str | None) -> str:
    if isinstance(step_field, dict):
        return str(step_field.get("name") or "").strip()
    return str(step_field or "").strip()


def _is_optional_step_field(step_field: dict[str, object] | str | None) -> bool:
    return isinstance(step_field, dict) and bool(step_field.get("is_optional"))


def _get_step_fields(
    step_fields: dict[str, list[str]] | list[dict[str, object]] | list[str],
    *,
    include_optional: bool = True,
) -> list[str]:
    if isinstance(step_fields, dict):
        fields = list(step_fields.get("required", []))
        if include_optional:
            fields.extend(step_fields.get("optional", []))
        return fields
    fields: list[str] = []
    for step_field in step_fields or []:
        if not include_optional and _is_optional_step_field(step_field):
            continue
        field_name = _get_step_field_name(step_field)
        if field_name:
            fields.append(field_name)
    return fields


def _humanize_field_name(field_name: str) -> str:
    text = str(field_name or "").strip().replace("_", " ")
    return text[:1].upper() + text[1:] if text else ""


def _strip_prompt_examples(label: str) -> str:
    cleaned = re.sub(
        r"\s*\((?:e\.g\.|eg\.?|for example|example:)[^)]+\)",
        "",
        str(label or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _normalize_internal_status_line(line: str) -> str:
    normalized = re.sub(r"[*_`]", "", str(line or ""))
    normalized = re.sub(r"^\s*[-*]\s*", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized


def _strip_internal_status_filler(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""

    filtered_lines = [
        line
        for line in text.splitlines()
        if _normalize_internal_status_line(line) not in INTERNAL_STATUS_FILLER_SENTENCES
    ]
    return "\n".join(filtered_lines).strip()


def _is_skip_like_value(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return False
    normalized = re.sub(r"\s+", "", str(value).strip()).casefold()
    return normalized in SKIP_LIKE_TEXT_VALUES


def _normalize_email(value) -> str:
    return str(value or "").strip().casefold()


def _extract_email_domain(value) -> str:
    normalized_email = _normalize_email(value)
    if "@" not in normalized_email:
        return ""
    return normalized_email.rsplit("@", 1)[-1]


def _is_internal_customer_contact_email(value) -> bool:
    domain = _extract_email_domain(value)
    if not domain:
        return False
    return any(
        domain == internal_domain or domain.endswith(f".{internal_domain}")
        for internal_domain in INTERNAL_CUSTOMER_CONTACT_EMAIL_DOMAINS
    )


def _has_internal_customer_contact_email(data: dict | None) -> bool:
    if not isinstance(data, dict):
        return False
    return _is_internal_customer_contact_email(data.get("contact_email"))


def _normalize_prompt_label_text(label: str) -> str:
    cleaned = _strip_prompt_examples(str(label or ""))
    cleaned = re.sub(r"\s*\(OPTIONAL\)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[*_`]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip().casefold()


def _build_generic_field_question(field_name: str, label: str) -> str:
    cleaned_label = _strip_prompt_examples(label) or _humanize_field_name(field_name)
    if not cleaned_label:
        return ""
    if field_name.endswith("_date"):
        return f"What is the {cleaned_label}?"
    if field_name == "sop_year":
        return f"What is the {cleaned_label}?"
    return f"What is the {cleaned_label}?"


FIELD_LABEL_QUESTION_LOOKUP = {
    _normalize_prompt_label_text(FIELD_LABELS[field_name]): (
        FIELD_QUESTION_OVERRIDES.get(field_name)
        or _build_generic_field_question(field_name, FIELD_LABELS[field_name])
    )
    for field_name in FIELD_LABELS
    if field_name not in AI_GENERATED_STEP_FIELDS
}


def _rewrite_questionless_field_prompt(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""

    lines = text.splitlines()
    first_non_empty_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_non_empty_index is None:
        return ""

    first_line = lines[first_non_empty_index].strip()
    if not first_line or first_line.endswith("?") or ":" in first_line:
        return text

    normalized_first_line = _normalize_prompt_label_text(first_line)
    replacement = FIELD_LABEL_QUESTION_LOOKUP.get(normalized_first_line)
    if not replacement:
        return text

    lines[first_non_empty_index] = replacement
    return "\n".join(lines).strip()


def _is_optional_field(field_name: str) -> bool:
    normalized_field_name = str(field_name or "").strip()
    if normalized_field_name in RFQ_OPTIONAL_FIELDS:
        return True
    product_field_match = re.fullmatch(
        r"products\[(\d+)\]\.([A-Za-z0-9_]+)",
        normalized_field_name,
    )
    return bool(
        product_field_match
        and product_field_match.group(2) in RFQ_OPTIONAL_PRODUCT_FIELDS
    )


def _format_field_for_prompt(field_name: str) -> str:
    normalized_field_name = str(field_name or "").strip()
    product_field_match = re.fullmatch(
        r"products\[(\d+)\]\.([A-Za-z0-9_]+)",
        normalized_field_name,
    )
    if product_field_match:
        product_index = product_field_match.group(1)
        product_field_name = product_field_match.group(2)
        base_label = _strip_prompt_examples(
            FIELD_LABELS.get(
                product_field_name,
                _humanize_field_name(product_field_name),
            )
        )
        label = f"Product {product_index} {base_label}"
    else:
        label = _strip_prompt_examples(
            FIELD_LABELS.get(
                normalized_field_name,
                _humanize_field_name(normalized_field_name),
            )
        )
    if _is_optional_field(normalized_field_name):
        label = f"{label} (OPTIONAL)"
    return label


def _format_field_list_for_prompt(field_names: list[str]) -> str:
    return json.dumps(
        [_format_field_for_prompt(field_name) for field_name in field_names],
        ensure_ascii=False,
    )


def _get_missing_fields_for_step(
    data: dict,
    step_fields: dict[str, list[str]] | list[str],
) -> list[str]:
    missing_fields: list[str] = []
    for field_name in _get_step_fields(step_fields, include_optional=True):
        if field_name == "costing_data":
            # costing_data is never surfaced as a generic missing field.
            # Whether to ask it depends on what retrieveProducts returns for the
            # selected product — handled exclusively via rule #6 in the system prompt.
            continue
        if field_name == "products":
            product_missing_fields = get_incomplete_product_fields(
                data,
                include_optional=True,
            )
            if product_missing_fields:
                missing_fields.extend(product_missing_fields)
            continue
        if not _is_field_filled(data, field_name):
            missing_fields.append(field_name)
    return missing_fields


def _get_missing_required_fields_for_step(
    data: dict,
    step_fields: dict[str, list[str]] | list[str],
) -> list[str]:
    missing_fields: list[str] = []
    for field_name in _get_step_fields(step_fields, include_optional=False):
        if field_name == "products":
            product_missing_fields = get_incomplete_product_fields(
                data,
                include_optional=False,
            )
            if product_missing_fields:
                missing_fields.extend(product_missing_fields)
            continue
        if not _is_field_filled(data, field_name, include_optional=False):
            missing_fields.append(field_name)
    return missing_fields


def _get_required_missing_fields_before_submission(
    data: dict,
) -> dict[int, list[str]]:
    missing_by_step: dict[int, list[str]] = {}
    for step_number, step_fields in RFQ_STEPS:
        if step_number >= 4:
            break
        step_missing_fields = _get_missing_required_fields_for_step(data, step_fields)
        if step_missing_fields:
            missing_by_step[step_number] = step_missing_fields
    return missing_by_step


def _sanitize_rfq_update_fields_for_chat(
    fields: dict[str, object],
) -> tuple[dict[str, object], list[str], list[str]]:
    sanitized_fields: dict[str, object] = {}
    rejected_required_fields: list[str] = []
    blocked_internal_contact_fields: list[str] = []

    if _is_internal_customer_contact_email(fields.get("contact_email")):
        blocked_internal_contact_fields.extend(
            field_name
            for field_name in RFQ_CONTACT_FIELDS
            if field_name in fields
        )

    for field_name, value in fields.items():
        if field_name in blocked_internal_contact_fields:
            continue

        if field_name == "products":
            normalized_products = normalize_rfq_data_products(
                {"products": value},
                products_authoritative=True,
            ).get("products", [])
            sanitized_products: list[dict[str, object]] = []
            for index, product in enumerate(normalized_products):
                next_product = dict(product)
                if _is_skip_like_value(next_product.get("part_number")):
                    next_product["part_number"] = None
                    rejected_required_fields.append(
                        f"products[{index}].part_number"
                    )
                if _is_skip_like_value(next_product.get("currency")):
                    next_product["currency"] = None
                    rejected_required_fields.append(
                        f"products[{index}].currency"
                    )
                if _is_skip_like_value(next_product.get("revision_level")):
                    next_product["revision_level"] = ""
                sanitized_products.append(next_product)
            sanitized_fields[field_name] = sanitized_products
            continue

        if field_name == "volumes":
            normalized_volumes = normalize_rfq_data_products(
                {"volumes": value}
            ).get("volumes", [])
            sanitized_volumes: list[dict[str, object]] = []
            for volume in normalized_volumes:
                next_volume = dict(volume)
                canonical_delivery_zone = normalize_delivery_zone(
                    next_volume.get("delivery_zone")
                )
                if canonical_delivery_zone:
                    next_volume["delivery_zone"] = canonical_delivery_zone
                sanitized_volumes.append(next_volume)
            sanitized_fields[field_name] = sanitized_volumes
            continue

        if _is_skip_like_value(value):
            if _is_optional_field(field_name):
                sanitized_fields[field_name] = "_"
            else:
                rejected_required_fields.append(field_name)
            continue

        sanitized_fields[field_name] = value

    return (
        sanitized_fields,
        rejected_required_fields,
        blocked_internal_contact_fields,
    )


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


def _document_type_label(value: RfqDocumentType | str | None) -> str:
    raw_value = value.value if isinstance(value, RfqDocumentType) else str(value or "")
    normalized = raw_value.strip().upper()
    if normalized == RfqDocumentType.RFI.value:
        return RfqDocumentType.RFI.value
    if normalized == RfqDocumentType.POTENTIAL.value:
        return RfqDocumentType.POTENTIAL.value
    return RfqDocumentType.RFQ.value


def _is_potential_document(rfq: Rfq) -> bool:
    return _document_type_label(rfq.document_type) == RfqDocumentType.POTENTIAL.value


def _format_document_type_text(content: str, document_type: RfqDocumentType | str | None) -> str:
    label = _document_type_label(document_type)
    if label == RfqDocumentType.RFQ.value:
        return str(content or "")
    return re.sub(r"\bRFQ\b", label, str(content or ""))


def _build_formal_initial_greeting(rfq: Rfq) -> str:
    return _format_document_type_text(ENGLISH_INITIAL_GREETING_TEMPLATE, rfq.document_type)


def _normalize_initial_greeting_for_document_type(history: list[dict], rfq: Rfq) -> list[dict]:
    if _document_type_label(rfq.document_type) == RfqDocumentType.RFQ.value:
        return history

    normalized_history = []
    replaced = False
    for message in history:
        next_message = dict(message)
        content = next_message.get("content")
        if (
            not replaced
            and next_message.get("role") == "assistant"
            and isinstance(content, str)
            and "Hello, I'm your sales assistant" in content
            and "RFQ" in content
        ):
            next_message["content"] = _format_document_type_text(content, rfq.document_type)
            replaced = True
        normalized_history.append(next_message)
    return normalized_history


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
            "the request workflow until you have either acknowledged one of the 6 "
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
    if _is_potential_document(rfq):
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

    _revision_products_rule = (
        "PRODUCTS UPDATE RULE: When the user updates any field inside a product row "
        "(e.g. target_price, quantity), call updateFormFields with only the changed "
        "fields for that row — the backend will merge them into the existing row "
        "automatically. After saving the product change, you MUST immediately call "
        "retrieveZoneManager (without passing to_total) so the backend recalculates "
        "the new TO and re-evaluates the validator assignment. Then save the returned "
        "zone_manager_email and validator_role via updateFormFields."
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
- {_revision_products_rule}
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
- {_revision_products_rule}
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
- {_revision_products_rule}
- DO NOT call submitValidation or any other tool that changes RFQ status.
- When the user says the updates are complete, instruct them to click the physical "Submit Updates" button at the top of their screen to send the RFQ back to the validator.
"""


def _is_field_filled(data: dict, field_name: str, *, include_optional: bool = True) -> bool:
    if field_name in RFQ_CONTACT_FIELDS and _has_internal_customer_contact_email(data):
        return False
    if field_name == "products":
        return not get_incomplete_product_fields(
            data,
            include_optional=include_optional,
        )
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
        stripped_value = value.strip()
        if not stripped_value:
            return False
        if _is_skip_like_value(stripped_value):
            return _is_optional_field(field_name)
        return True
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return value is not None


def _get_current_step_and_missing_fields(chat_mode: str, data: dict) -> tuple[int, list[str]]:
    if chat_mode == "potential":
        steps = POTENTIAL_STEPS
    else:
        steps = RFQ_STEPS
    for step_number, fields in steps:
        missing_fields = (
            [
                field
                for field in fields
                if not _is_field_filled(data, field)
            ]
            if chat_mode == "potential"
            else _get_missing_fields_for_step(data, fields)
        )
        if missing_fields:
            return step_number, missing_fields
    return steps[-1][0], []


def _history_uses_paragraph_mode(history: list[dict] | None) -> bool:
    for message in history or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        if str(message.get("content") or "").strip() == RFQ_PARAGRAPH_MODE_PROMPT:
            return True
    return False


def _should_prioritize_rfq_files(
    chat_mode: str,
    current_step: int,
    user_keys_missing: list[str],
) -> bool:
    if chat_mode == "potential" or current_step != 1 or "rfq_files" not in user_keys_missing:
        return False

    step_1_fields = next(
        (fields for step_number, fields in RFQ_STEPS if step_number == 1),
        [],
    )
    ordered_step_1_fields = _get_step_fields(
        step_1_fields,
        include_optional=True,
    )
    try:
        rfq_files_index = ordered_step_1_fields.index("rfq_files")
    except ValueError:
        return False

    fields_before_rfq_files = {
        field_name
        for field_name in ordered_step_1_fields[:rfq_files_index]
        if field_name != "costing_data"
    }
    return not any(
        missing_field in fields_before_rfq_files
        for missing_field in user_keys_missing
    )


def _build_missing_fields_prompt(
    chat_mode: str,
    data: dict,
    *,
    prioritize_rfq_files: bool = False,
) -> str:
    if chat_mode == "potential":
        steps = POTENTIAL_STEPS
    else:
        steps = RFQ_STEPS
    current_step, missing_fields = _get_current_step_and_missing_fields(chat_mode, data)
    current_step_fields = next(
        (fields for step_number, fields in steps if step_number == current_step),
        {} if chat_mode != "potential" else [],
    )
    current_step_field_names = _get_step_fields(
        current_step_fields,
        include_optional=False,
    )
    filled_fields = [
        field
        for field in current_step_field_names
        if _is_field_filled(data, field, include_optional=False)
    ]
    user_keys_missing = [key for key in missing_fields if key not in AI_GENERATED_STEP_FIELDS]
    ai_keys_missing = [key for key in missing_fields if key in AI_GENERATED_STEP_FIELDS]
    prioritize_rfq_files_now = (
        prioritize_rfq_files
        and _should_prioritize_rfq_files(
            chat_mode,
            current_step,
            user_keys_missing,
        )
    )
    if prioritize_rfq_files_now:
        user_keys_missing = ["rfq_files"] + [
            key for key in user_keys_missing if key != "rfq_files"
        ]
    next_field_to_ask = user_keys_missing[0] if user_keys_missing else (
        ai_keys_missing[0] if ai_keys_missing else ""
    )
    prompt = (
        f"STATE RECONCILIATION FOR STEP {current_step}:\n"
        f"- Fields already present and MUST NOT be asked again: {_format_field_list_for_prompt(filled_fields)}\n"
        f"- Missing fields you must ASK THE USER for: {_format_field_list_for_prompt(user_keys_missing)}\n"
        f"- Missing fields YOU MUST GENERATE/CALCULATE yourself: {_format_field_list_for_prompt(ai_keys_missing)}"
    )
    if next_field_to_ask and _PRODUCTS_VOLUME_FIELD_RE.match(next_field_to_ask):
        prompt += (
            "\n- MULTI-PRODUCT COLLECTION CHECK: The next missing field is a Volumes table "
            "field (quantity / target_price / currency / target_price_is_estimated). "
            "These fields are collected in Step 5, NOT here. "
            "Per the MULTI-PRODUCT COLLECTION RULE, before moving to Step 5 you MUST first "
            "ask: \"Would you like to add another product to this request?\" "
            "Only after the user says NO should you proceed to collect volumes."
        )
    elif next_field_to_ask:
        prompt += (
            "\n- Next field to ask for: "
            f"{_format_field_for_prompt(next_field_to_ask)}"
        )
        preferred_question = FIELD_QUESTION_OVERRIDES.get(next_field_to_ask)
        if preferred_question:
            if _is_optional_field(next_field_to_ask) and "optional" not in preferred_question.lower():
                preferred_question = (
                    f"{preferred_question}\n\n*(Optional — type **skip** to leave it blank.)*"
                )
            prompt += (
                "\n- Preferred exact wording for the next question: "
                f"{json.dumps(preferred_question, ensure_ascii=False)}"
            )
        prompt += (
            "\n- When asking the next field, you MUST write a real question in natural "
            "language. Never output only the bare field label."
        )
    if prioritize_rfq_files_now:
        prompt += (
            "\n- PARAGRAPH MODE FILE BLOCKER: `rfq_files` is still missing. "
            "You MUST ask for the RFQ files upload before any later remaining Step 1 field."
        )
    if not missing_fields:
        prompt += (
            "\nAll required fields for this step are already present, so move to the next "
            "workflow action instead of re-asking completed fields."
        )

    # ── Strict Step 4 blocking when Steps 2 or 3 are incomplete ──
    if chat_mode != "potential" and current_step < 4:
        step2_fields = next(
            (fields for step_number, fields in steps if step_number == 2), {}
        )
        step3_fields = next(
            (fields for step_number, fields in steps if step_number == 3), {}
        )
        step2_missing = _get_missing_required_fields_for_step(data, step2_fields)
        step3_missing = _get_missing_required_fields_for_step(data, step3_fields)

        if step2_missing or step3_missing:
            prompt += (
                "\n\n*** STEP 4 IS BLOCKED ***"
                "\nStep 4 (Validation & Submission) is COMPLETELY HIDDEN from you."
                "\nYou are FORBIDDEN from mentioning 'Validation', 'Submit', 'Step 4', "
                "'Turnover', 'TO Total', or 'Zone Manager' until Steps 2 AND 3 are 100% complete."
            )
            if step2_missing:
                prompt += (
                    "\n- Step 2 still missing: "
                    f"{_format_field_list_for_prompt(step2_missing)}"
                )
            if step3_missing:
                prompt += (
                    "\n- Step 3 still missing: "
                    f"{_format_field_list_for_prompt(step3_missing)}"
                )
            prompt += (
                "\nYou MUST ask about the current step's missing fields ONLY. "
                "If the current step is Step 1 and it is complete, move to Step 2 "
                "(Delivery Conditions, Payment Terms, Packaging, etc.) immediately."
            )

    return prompt


def _filter_update_fields(chat_mode: str, fields: dict) -> dict:
    if chat_mode == "potential":
        allowed_fields = POTENTIAL_ALLOWED_FIELDS
    else:
        allowed_fields = RFQ_ALLOWED_FIELDS
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


def _try_load_exact_json_payload(content: str):
    text = _strip_fenced_payload(content)
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


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
        parsed_payload = _try_load_exact_json_payload(candidate)
        if parsed_payload is not None:
            return parsed_payload
    return None


def _normalize_external_tool_response_content(
    tool_name: str,
    response_text: str,
) -> str:
    parsed_payload = _try_load_json_payload(response_text)
    if parsed_payload is not None:
        return json.dumps(parsed_payload)

    logger.warning(
        "Tool %s returned non-JSON content. Falling back to a safe tool payload.",
        tool_name,
    )

    fallback_payloads = {
        "checkGroupeExistence": {
            "exists": False,
            "matches": [],
            "tool_error": "invalid_json_response",
        },
        "retrieveProducts": {
            "products": [],
            "tool_error": "invalid_json_response",
        },
        "checkContactExistence": {
            "exists": False,
            "contact": None,
            "tool_error": "invalid_json_response",
        },
    }
    return json.dumps(
        fallback_payloads.get(
            tool_name,
            {"success": False, "tool_error": "invalid_json_response"},
        )
    )


def _is_technical_parse_error_line(line: str) -> bool:
    normalized = str(line or "").strip().lower()
    if re.match(r"(?i)^\s*failed to parse as json\s*:", normalized):
        return True
    # LLM self-reports for invalid tool call JSON or other API-level errors
    if normalized.startswith("oops"):
        return True
    # LLM leaks internal silent-update instructions (e.g. "No costing parameters; set costingdata "" silently.")
    if re.search(r"silently\.?\s*$", normalized):
        return True
    if re.search(r"tool call arguments must be valid json", normalized):
        return True
    if re.match(r"^\s*error\s*:\s*(tool|function|call|json|parse)", normalized):
        return True
    return False


def _strip_technical_error_lines(content: str) -> str:
    lines = str(content or "").splitlines()
    filtered_lines = [
        line
        for line in lines
        if not _is_technical_parse_error_line(line)
    ]
    return "\n".join(filtered_lines).strip()


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
        # ── Preserve the append_products flag before flattening ──
        raw_append = None
        if "append_products" in normalized:
            raw_append = normalized.pop("append_products")
        elif "appendProducts" in normalized:
            raw_append = normalized.pop("appendProducts")
        fields = normalized.get("fields_to_update")
        if not isinstance(fields, dict):
            fields = {
                key: value
                for key, value in normalized.items()
                if key not in ("fields_to_update", "append_products", "appendProducts")
            }
        legacy_scope = fields.pop("is_feasible", None) if isinstance(fields, dict) else None
        if isinstance(fields, dict) and legacy_scope is not None and "scope" not in fields:
            fields["scope"] = _normalize_scope_value(legacy_scope)
        normalized_fields = {}
        if isinstance(fields, dict):
            for key, value in fields.items():
                normalized_fields[UPDATE_FORM_FIELD_ALIASES.get(key, key)] = value
        result = {"fields_to_update": normalized_fields}
        if raw_append is not None:
            result["append_products"] = str(raw_append).strip().lower() in ("true", "1", "yes") if not isinstance(raw_append, bool) else raw_append
        normalized = result
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
        "fieldstoupdate",
        "fields_to_update",
        "appendproducts",
        "append_products",
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


def _get_available_tools(rfq: Rfq, chat_mode: str) -> list[dict]:
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

    parsed_payload = _try_load_exact_json_payload(text)
    if parsed_payload is None:
        return False
    return isinstance(parsed_payload, (dict, list))


def _strip_fenced_json_blocks(content: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        payload = _try_load_exact_json_payload(match.group(1))
        if isinstance(payload, (dict, list)):
            return ""
        return match.group(0)

    return re.sub(r"```(?:json)?\s*([\s\S]*?)```", _replace, content, flags=re.IGNORECASE)


def _find_json_block_end(content: str, start_index: int) -> int | None:
    opening = content[start_index]
    closing = "}" if opening == "{" else "]"
    stack = [closing]
    in_string = False
    escape_next = False

    for index in range(start_index + 1, len(content)):
        char = content[index]
        if in_string:
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            stack.append("}")
            continue
        if char == "[":
            stack.append("]")
            continue
        if char in {"}", "]"}:
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return index + 1

    return None


def _strip_standalone_json_blocks(content: str) -> str:
    text = content
    ranges_to_remove: list[tuple[int, int]] = []
    index = 0

    while index < len(text):
        if text[index] not in "{[":
            index += 1
            continue

        end_index = _find_json_block_end(text, index)
        if end_index is None:
            index += 1
            continue

        payload = _try_load_exact_json_payload(text[index:end_index])
        if not isinstance(payload, (dict, list)):
            index += 1
            continue

        line_start = text.rfind("\n", 0, index) + 1
        line_end = text.find("\n", end_index)
        if line_end == -1:
            line_end = len(text)

        if text[line_start:index].strip() or text[end_index:line_end].strip():
            index += 1
            continue

        ranges_to_remove.append((line_start, line_end))
        index = line_end + 1

    if not ranges_to_remove:
        return text

    cleaned_parts: list[str] = []
    cursor = 0
    for start_index, end_index in ranges_to_remove:
        cleaned_parts.append(text[cursor:start_index])
        cursor = end_index
    cleaned_parts.append(text[cursor:])
    return "".join(cleaned_parts)


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

    text = _strip_internal_status_filler(text).strip()
    if not text:
        return ""

    text = _strip_scratchpad_reasoning(text).strip()
    if not text:
        return ""

    text = _strip_technical_error_lines(text).strip()
    if not text:
        return ""

    text = _strip_fenced_json_blocks(text).strip()
    if _is_internal_tool_payload_text(text):
        return ""

    text = _strip_standalone_json_blocks(text).strip()
    if _is_internal_tool_payload_text(text):
        return ""
    text = _dedupe_adjacent_blocks(text)
    return _rewrite_questionless_field_prompt(text)


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


def _extract_successful_submit_validation_payload(
    tool_messages: list[dict] | None,
) -> dict | None:
    for tool_message in tool_messages or []:
        if not isinstance(tool_message, dict):
            continue
        if tool_message.get("name") != "submitValidation":
            continue
        payload = _try_load_json_payload(tool_message.get("content") or "")
        if isinstance(payload, dict) and payload.get("success") is True:
            return payload
    return None


def _build_submit_validation_success_text(
    rfq: Rfq,
    payload: dict | None = None,
) -> str:
    document_label = _document_type_label(rfq.document_type)
    sub_status = str(
        (payload or {}).get("sub_status")
        or RfqSubStatus.PENDING_FOR_VALIDATION.value
    ).strip() or RfqSubStatus.PENDING_FOR_VALIDATION.value
    return (
        f"Your {document_label} was submitted and is now {sub_status}. "
        "The validation workflow has started."
    )


def _build_submit_validation_question(rfq: Rfq) -> str:
    document_label = _document_type_label(rfq.document_type)
    return (
        f"Do you want to submit this {document_label} for validation?\n\n"
        "Yes\n"
        "No"
    )


def _build_modify_before_submission_question() -> str:
    return PRE_SUBMISSION_MODIFY_PROMPT


def _build_modify_fields_follow_up_text() -> str:
    return "Please tell me which field you would like to update or modify."


def _build_submit_later_text() -> str:
    return "No problem! You can submit the RFQ for validation whenever you're ready."


_NEUTRAL_ACK_RE = re.compile(
    r"^\s*(?:ok(?:ay)?|alright|sure|got\s+it|noted|understood|thanks?|thank\s+you|perfect|great|fine|cool|sounds?\s+good)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _is_submit_later_ack(last_assistant_text: str, user_message: str) -> bool:
    """True when the user is just acknowledging the 'submit later' reply."""
    return (
        _normalize_prompt_block_text(last_assistant_text)
        == _normalize_prompt_block_text(_build_submit_later_text())
        and bool(_NEUTRAL_ACK_RE.match(user_message))
    )


def _normalize_prompt_block_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


def _normalize_yes_no_reply(message: str | None) -> str | None:
    normalized = _normalize_prompt_block_text(message)
    if normalized in {"yes", "y", "1"}:
        return "yes"
    if normalized in {"no", "n", "2"}:
        return "no"
    return None


def _get_last_visible_assistant_text(history: list[dict] | None) -> str:
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "assistant":
            continue
        if entry.get("tool_calls"):
            continue
        content = str(entry.get("content") or "").strip()
        if content:
            return content
    return ""


def _match_pre_submission_prompt_action(
    *,
    last_assistant_text: str,
    user_message: str,
    rfq: Rfq,
) -> str | None:
    reply = _normalize_yes_no_reply(user_message)
    if reply is None:
        return None

    normalized_last_assistant_text = _normalize_prompt_block_text(last_assistant_text)
    normalized_modify_prompt = _normalize_prompt_block_text(
        _build_modify_before_submission_question()
    )
    normalized_submit_prompt = _normalize_prompt_block_text(
        _build_submit_validation_question(rfq)
    )

    if (
        normalized_modify_prompt in normalized_last_assistant_text
        or _MODIFY_QUESTION_RE.search(last_assistant_text)
    ):
        return f"modify_{reply}"
    if (
        normalized_submit_prompt in normalized_last_assistant_text
        or _SUBMIT_QUESTION_RE.search(last_assistant_text)
    ):
        return f"submit_{reply}"
    return None


def _is_waiting_for_pre_submission_modify_details(last_assistant_text: str | None) -> bool:
    return (
        _normalize_prompt_block_text(last_assistant_text)
        == _normalize_prompt_block_text(_build_modify_fields_follow_up_text())
    )


def _is_pre_submission_modify_turn(
    *,
    last_assistant_text: str,
    user_message: str,
    rfq: Rfq,
) -> bool:
    if _is_waiting_for_pre_submission_modify_details(last_assistant_text):
        return True

    if _normalize_yes_no_reply(user_message) is not None:
        return False

    normalized_last_assistant_text = _normalize_prompt_block_text(last_assistant_text)
    normalized_modify_prompt = _normalize_prompt_block_text(
        _build_modify_before_submission_question()
    )
    normalized_submit_prompt = _normalize_prompt_block_text(
        _build_submit_validation_question(rfq)
    )

    return (
        normalized_modify_prompt in normalized_last_assistant_text
        or _MODIFY_QUESTION_RE.search(last_assistant_text)
        or normalized_submit_prompt in normalized_last_assistant_text
        or _SUBMIT_QUESTION_RE.search(last_assistant_text)
    )


def _tool_messages_include_clean_update_form_success(
    tool_messages: list[dict] | None,
) -> bool:
    for tool_message in tool_messages or []:
        if not isinstance(tool_message, dict):
            continue
        if tool_message.get("name") != "updateFormFields":
            continue
        payload = _try_load_json_payload(tool_message.get("content") or "")
        if not isinstance(payload, dict):
            continue
        if payload.get("success") is not True:
            continue
        if payload.get("status") != "extracted_to_form":
            continue
        updated_fields = payload.get("fields_updated") or []
        if isinstance(updated_fields, list) and updated_fields:
            return True
    return False


def _rewrite_submit_prompt_to_modify_prompt_if_needed(
    *,
    text: str,
    rfq: Rfq,
    chat_mode: str,
    extracted_data: dict,
) -> str:
    if rfq.sub_status == RfqSubStatus.PENDING_FOR_VALIDATION:
        return text

    current_step, missing_fields = _get_current_step_and_missing_fields(
        chat_mode,
        extracted_data,
    )
    if missing_fields or current_step < 4:
        return text

    submit_prompt = _build_submit_validation_question(rfq)
    if submit_prompt not in text:
        return text

    return text.replace(
        submit_prompt,
        _build_modify_before_submission_question(),
        1,
    )


def _tool_messages_indicate_internal_contact_blocked(
    tool_messages: list[dict] | None,
) -> bool:
    for tool_message in tool_messages or []:
        if not isinstance(tool_message, dict):
            continue
        payload = _try_load_json_payload(tool_message.get("content") or "")
        if not isinstance(payload, dict):
            continue
        if payload.get("internal_contact") is True:
            return True
        if payload.get("status") == "internal_contact_blocked":
            return True
        blocked_fields = payload.get("blocked_internal_contact_fields") or []
        if isinstance(blocked_fields, list) and any(
            field_name in RFQ_CONTACT_FIELDS for field_name in blocked_fields
        ):
            return True
    return False


def _text_explains_internal_contact_rejection(content: str | None) -> bool:
    normalized = str(content or "").casefold()
    return (
        "avocarbon" in normalized
        and "contact" in normalized
        and ("internal" in normalized or "cannot be used" in normalized)
    )


def _build_internal_contact_rejection_text(
    *,
    rfq: Rfq,
    extracted_data: dict,
) -> str:
    return (
        "That email is an internal Avocarbon address and cannot be used as a "
        "customer contact.\n\n"
        + _build_field_question_with_options(
            rfq=rfq,
            field_name="contact_email",
            extracted_data=extracted_data,
        )
    )


def _build_products_collection_fallback_text(
    *,
    rfq: Rfq,
    product_index: int = 1,
) -> str:
    ordinal_label = "first" if product_index <= 1 else "next"
    return (
        f"Please provide the {ordinal_label} product details (one line item):\n\n"
        "Product\n"
        "Product Line\n"
        "Costing Data (optional - you may omit it)\n"
        "Application\n"
        "Part Number\n"
        "Revision Level (optional — you may omit it)\n\n"
        "After the part number, you will be asked to upload the Drawing and provide the SOP Year."
    )


def _build_field_question_with_options(
    *,
    rfq: Rfq,
    field_name: str,
    extracted_data: dict,
) -> str:
    normalized_field_name = str(field_name or "").strip()
    question = FIELD_QUESTION_OVERRIDES.get(normalized_field_name)
    if not question:
        question = _build_generic_field_question(
            normalized_field_name,
            FIELD_LABELS.get(
                normalized_field_name,
                _humanize_field_name(normalized_field_name),
            ),
        )

    if _is_optional_field(normalized_field_name):
        question = f"{question}\n\n(Optional - type skip to leave it blank.)"

    if normalized_field_name == "rfq_files":
        return f"{question}\n\nYes\nNo"
    if normalized_field_name == "automotive_type":
        return f"{question}\n\n" + "\n".join(AUTOMOTIVE_TYPE_OPTIONS)
    if normalized_field_name == "delivery_zone":
        return f"{question}\n\n" + "\n".join(APPROVED_DELIVERY_ZONES)
    if normalized_field_name == "type_of_packaging":
        return f"{question}\n\n" + "\n".join(TYPE_OF_PACKAGING_OPTIONS)
    if normalized_field_name == "target_price_is_estimated":
        return f"{question}\n\n" + "\n".join(PRICE_SOURCE_OPTIONS)

    product_field_match = re.fullmatch(
        r"products\[(\d+)\]\.([A-Za-z0-9_]+)",
        normalized_field_name,
    )
    if product_field_match:
        product_index = int(product_field_match.group(1))
        base_field_name = product_field_match.group(2)
        products = (
            extracted_data.get("products")
            if isinstance(extracted_data.get("products"), list)
            else []
        )
        product = (
            products[product_index - 1]
            if 0 <= product_index - 1 < len(products)
            and isinstance(products[product_index - 1], dict)
            else {}
        )
        part_number = str(product.get("part_number") or "").strip()
        currency = str(
            product.get("currency")
            or extracted_data.get("target_price_currency")
            or ""
        ).strip().upper()

        if base_field_name == "target_price_is_estimated":
            if part_number:
                question = f"What is the Price source for part number {part_number}?"
            else:
                question = "What is the Price source for this part number?"
            return f"{question}\n\n" + "\n".join(PRICE_SOURCE_OPTIONS)

        if base_field_name == "target_price":
            if part_number and currency:
                return (
                    "What is the Target price (numeric value) for part number "
                    f"{part_number} ({currency})?"
                )
            if part_number:
                return f"What is the Target price for part number {part_number}?"

        if part_number:
            label = FIELD_LABELS.get(
                base_field_name,
                _humanize_field_name(base_field_name),
            )
            return f"What is the {label} for part number {part_number}?"

        return _build_products_collection_fallback_text(
            rfq=rfq,
            product_index=product_index,
        )

    return question


def _build_user_facing_fallback_text(
    *,
    rfq: Rfq,
    chat_mode: str,
    extracted_data: dict,
    user_message: str = "",
    force_internal_contact_explanation: bool = False,
) -> str:
    if rfq.sub_status == RfqSubStatus.PENDING_FOR_VALIDATION:
        document_label = _document_type_label(rfq.document_type)
        return f"Your {document_label} is waiting for validator approval."

    current_step, missing_fields = _get_current_step_and_missing_fields(
        chat_mode,
        extracted_data,
    )
    user_missing_fields = [
        field_name
        for field_name in missing_fields
        if field_name not in AI_GENERATED_STEP_FIELDS
        and field_name not in SYSTEM_MANAGED_CHAT_FIELDS
    ]

    if user_missing_fields:
        next_field = user_missing_fields[0]
        product_field_match = re.fullmatch(
            r"products\[(\d+)\]\.([A-Za-z0-9_]+)",
            next_field,
        )
        if product_field_match:
            product_index = int(product_field_match.group(1))
            same_product_missing_fields = [
                field_name
                for field_name in user_missing_fields
                if field_name.startswith(f"products[{product_index}].")
            ]
            if len(same_product_missing_fields) > 1:
                return _build_products_collection_fallback_text(
                    rfq=rfq,
                    product_index=product_index,
                )
        if next_field == "products":
            return _build_products_collection_fallback_text(rfq=rfq)
        if next_field == "rfq_files" and _message_contains_url(user_message):
            return _build_rfq_files_url_rejection_text(rfq=rfq, extracted_data=extracted_data)
        if next_field == "contact_email" and force_internal_contact_explanation:
            return _build_internal_contact_rejection_text(
                rfq=rfq,
                extracted_data=extracted_data,
            )
        if next_field == "contact_email" and any(
            f"@{domain}" in user_message.casefold()
            for domain in INTERNAL_CUSTOMER_CONTACT_EMAIL_DOMAINS
        ):
            return (
                "That's an internal Avocarbon address — it cannot be used as a customer contact.\n\n"
                "What is the Contact email?"
            )
        return _build_field_question_with_options(
            rfq=rfq,
            field_name=next_field,
            extracted_data=extracted_data,
        )

    if not missing_fields and current_step >= 4:
        return _build_modify_before_submission_question()

    return "Please send your last answer again."


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


def _message_contains_url(message: str | None) -> bool:
    return bool(URL_PATTERN.search(str(message or "")))


def _build_rfq_files_url_rejection_text(*, rfq: Rfq, extracted_data: dict) -> str:
    follow_up_question = _build_field_question_with_options(
        rfq=rfq,
        field_name="rfq_files",
        extracted_data=extracted_data,
    )
    return (
        "I can't accept a URL or link as an RFQ file.\n\n"
        "Please upload the drawing/spec file directly using the Attach files button, "
        "then confirm here once the file is uploaded.\n\n"
        f"{follow_up_question}"
    )


def _should_reject_rfq_file_url_message(
    *,
    chat_mode: str,
    extracted_data: dict,
    user_message: str,
) -> bool:
    if chat_mode == "potential" or not _message_contains_url(user_message):
        return False

    current_step, missing_fields = _get_current_step_and_missing_fields(
        chat_mode,
        extracted_data,
    )
    if current_step != 1 or not missing_fields:
        return False

    user_missing_fields = [
        field_name
        for field_name in missing_fields
        if field_name not in AI_GENERATED_STEP_FIELDS
        and field_name not in SYSTEM_MANAGED_CHAT_FIELDS
    ]
    if not user_missing_fields or user_missing_fields[0] != "rfq_files":
        return False

    return not _is_field_filled(extracted_data, "rfq_files")


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

        if next_message.get("role") == "assistant" and isinstance(content, str):
            sanitized_content = _sanitize_assistant_text(content)
            if not sanitized_content:
                continue
            next_message["content"] = sanitized_content

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


def _make_json_safe(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _make_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_make_json_safe(item) for item in value]
    return str(value)


def _build_chat_persistence_error_text() -> str:
    return (
        "**System error.**\n\n"
        "- The assistant could not save this chat turn.\n"
        "- Please try again.\n"
    )


async def _persist_chat_state(
    *,
    db: AsyncSession,
    rfq: Rfq,
    extracted_data: dict,
    history: list[dict],
    chat_mode: str,
) -> bool:
    try:
        safe_history = _make_json_safe(history)
        safe_data = _make_json_safe(dict(extracted_data or {}))

        if chat_mode == "potential":
            safe_data["potential_chat_history"] = safe_history
            rfq.rfq_data = safe_data
        else:
            rfq.chat_history = safe_history
            rfq.rfq_data = _normalize_rfq_data_fields(safe_data)

        await db.commit()
        return True
    except Exception:
        await db.rollback()
        logger.exception("Failed to persist chat state for RFQ %s.", rfq.rfq_id)
        return False


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
            tool_response_text = _normalize_external_tool_response_content(
                func_name,
                resp.text,
            )

        elif func_name == "retrieveProducts":
            prod_name = args.get("productName", "")
            resp = await http_client.get(
                f"{BASE_URL}/api/products",
                params={"productName": prod_name},
            )
            tool_response_text = _normalize_external_tool_response_content(
                func_name,
                resp.text,
            )

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
            if email and _is_internal_customer_contact_email(email):
                tool_response_text = json.dumps(
                    {
                        "exists": False,
                        "internal_contact": True,
                        "message": (
                            "Internal Avocarbon email addresses are not valid "
                            "customer contacts."
                        ),
                    }
                )
            elif email:
                extracted_data["contact_email"] = email
                resp = await http_client.get(
                    f"{BASE_URL}/api/contact/check",
                    params={"email": email},
                )
                tool_response_text = _normalize_external_tool_response_content(
                    func_name,
                    resp.text,
                )
            else:
                tool_response_text = json.dumps(
                    {
                        "exists": False,
                        "error": "contact_email is required",
                    }
                )

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
                extracted_data.update(normalize_rfq_data_products(extracted_data))
                incomplete_product_fields = get_incomplete_product_fields(extracted_data)
                if incomplete_product_fields:
                    raise ValueError(
                        "Complete products before validator routing: "
                        + ", ".join(incomplete_product_fields)
                    )

                products = (
                    extracted_data.get("products")
                    if isinstance(extracted_data.get("products"), list)
                    else []
                )
                total_target_to = _coerce_numeric_value(
                    extracted_data.get("total_target_to")
                )
                if total_target_to <= 0:
                    raise ValueError(
                        "total_target_to must be greater than zero before validator routing."
                    )

                extracted_data["total_target_to"] = total_target_to
                first_product = products[0] if products else {}
                shared_currency = str(
                    first_product.get("currency")
                    or extracted_data.get("target_price_currency")
                    or "EUR"
                ).strip().upper() or "EUR"
                extracted_data["target_price_currency"] = shared_currency

                first_product_price = first_product.get("target_price")
                if first_product_price not in (None, ""):
                    extracted_data["target_price_local"] = str(first_product_price)

                if shared_currency != "EUR":
                    if db3 is None:
                        raise ValueError(
                            f"FX lookup is unavailable for {shared_currency}. "
                            "Please ask the user to restate the Target Price directly in EUR."
                        )
                    eur_rate = await get_eur_exchange_rate(shared_currency, db3=db3)
                    fallback_used = bool(shared_currency and eur_rate == 1.0)
                    if fallback_used:
                        raise ValueError(
                            f"FX lookup fallback prevented validator routing for {shared_currency}. "
                            "Please ask the user to restate the Target Price directly in EUR."
                        )
                    extracted_data["to_total_local"] = str(total_target_to / 1000.0)
                    to_total_float = (total_target_to * eur_rate) / 1000.0
                else:
                    extracted_data.pop("to_total_local", None)
                    if first_product_price not in (None, ""):
                        extracted_data["target_price_eur"] = str(first_product_price)
                    to_total_float = total_target_to / 1000.0

                extracted_data["to_total"] = str(to_total_float)
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
                                        "delivery_zone must be one of: "
                                        f"{', '.join(APPROVED_DELIVERY_ZONES)}."
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
                        # Mirror to model attribute so the form field is populated
                        # immediately when the frontend calls getRfq after this turn.
                        rfq.zone_manager_email = zone_manager_email

                    # Signal the frontend that form fields changed this turn.
                    if "updateFormFields" not in tool_calls_used:
                        tool_calls_used.append("updateFormFields")

                    tool_response_text = json.dumps(
                        {
                            "role_assigned": required_role,
                            "validator_role": required_role,
                            "validator_email": zone_manager_email,
                            "zone_manager_email": zone_manager_email,
                            "products": extracted_data.get("products"),
                            "total_target_to": total_target_to,
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
                missing_required_fields_by_step = _get_required_missing_fields_before_submission(
                    saved_data
                )
                if missing_required_fields_by_step:
                    flattened_missing_required_fields = [
                        field_name
                        for step_fields in missing_required_fields_by_step.values()
                        for field_name in step_fields
                    ]
                    tool_response_text = json.dumps(
                        {
                            "success": False,
                            "error": (
                                "All required fields from Steps 1 to 3 must be completed "
                                "before submitValidation can run."
                            ),
                            "missing_fields": flattened_missing_required_fields,
                            "missing_fields_by_step": missing_required_fields_by_step,
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

                validator_email = str(saved_data.get("zone_manager_email") or rfq.zone_manager_email or "").strip()
                to_total_value = str(saved_data.get("to_total") or "").strip()
                total_target_to_value = str(saved_data.get("total_target_to") or "").strip()
                product_line_acronym = str(
                    saved_data.get("product_line_acronym")
                    or rfq.product_line_acronym
                    or ""
                ).strip()

                missing_step4_fields = [
                    field_name
                    for field_name, field_value in (
                        ("products", "" if get_incomplete_product_fields(saved_data) else "ok"),
                        ("total_target_to", total_target_to_value),
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
            if "automotive_type" in fields:
                normalized_automotive_type = _normalize_automotive_type(
                    fields["automotive_type"]
                )
                if normalized_automotive_type:
                    fields["automotive_type"] = normalized_automotive_type
            if "target_price_is_estimated" in fields:
                val = fields["target_price_is_estimated"]
                fields["target_price_is_estimated"] = (
                    val if isinstance(val, bool)
                    else str(val).strip().lower() in ("true", "1", "yes")
                )
            filtered_fields = _filter_update_fields(chat_mode, fields)
            rejected_required_fields: list[str] = []
            blocked_internal_contact_fields: list[str] = []
            if chat_mode != "potential":
                (
                    filtered_fields,
                    rejected_required_fields,
                    blocked_internal_contact_fields,
                ) = (
                    _sanitize_rfq_update_fields_for_chat(filtered_fields)
                )
            if "delivery_zone" in filtered_fields:
                canonical_delivery_zone = normalize_delivery_zone(
                    filtered_fields.get("delivery_zone")
                )
                if canonical_delivery_zone:
                    filtered_fields["delivery_zone"] = canonical_delivery_zone

            # ── append_products: merge new product rows into existing ones ──
            should_append = args.get("append_products") is True
            if should_append and "products" in filtered_fields:
                persisted_rfq_data = _normalize_rfq_data_fields(
                    getattr(rfq, "rfq_data", None)
                )
                persisted_products = (
                    persisted_rfq_data.get("products")
                    if isinstance(persisted_rfq_data.get("products"), list)
                    else []
                )
                in_memory_products = (
                    extracted_data.get("products")
                    if isinstance(extracted_data.get("products"), list)
                    else []
                )
                existing_products = persisted_products or in_memory_products
                normalized_new_products = normalize_rfq_data_products(
                    {"products": filtered_fields["products"]},
                    products_authoritative=True,
                ).get("products", [])
                if isinstance(existing_products, list) and isinstance(normalized_new_products, list) and existing_products:
                    # Reject mixed currencies
                    def _product_currency(p):
                        return str(p.get("currency") or "").strip().upper()
                    existing_currencies = {_product_currency(p) for p in existing_products if _product_currency(p)}
                    new_currencies = {_product_currency(p) for p in normalized_new_products if _product_currency(p)}
                    all_currencies = existing_currencies | new_currencies
                    if len(all_currencies) > 1:
                        tool_response_text = json.dumps({
                            "success": False,
                            "error": "All product rows must use the same currency. "
                                     f"Existing: {existing_currencies}, New: {new_currencies}",
                        })
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": func_name,
                            "content": tool_response_text,
                        })
                        continue
                if isinstance(existing_products, list) and isinstance(normalized_new_products, list):
                    filtered_fields["products"] = existing_products + normalized_new_products
                    # Keep volumes aligned with the new products count — pad with empty rows
                    persisted_volumes = (
                        persisted_rfq_data.get("volumes")
                        if isinstance(persisted_rfq_data.get("volumes"), list)
                        else (
                            extracted_data.get("volumes")
                            if isinstance(extracted_data.get("volumes"), list)
                            else []
                        )
                    )
                    new_product_count = len(filtered_fields["products"])
                    while len(persisted_volumes) < new_product_count:
                        persisted_volumes.append({})
                    filtered_fields["volumes"] = persisted_volumes

            # ── smart-merge: patch existing product rows rather than replacing them ──
            # When the LLM sends a partial products update (e.g., only target_price),
            # merge the changed fields into the existing rows matched by part_number so
            # that all other product fields (quantity, revision_level, costing_data, …)
            # are preserved.
            if not should_append and "products" in filtered_fields:
                persisted_rfq_data = _normalize_rfq_data_fields(
                    getattr(rfq, "rfq_data", None)
                )
                _existing = (
                    persisted_rfq_data.get("products")
                    if isinstance(persisted_rfq_data.get("products"), list) and persisted_rfq_data.get("products")
                    else (
                        extracted_data.get("products")
                        if isinstance(extracted_data.get("products"), list) and extracted_data.get("products")
                        else None
                    )
                )
                if _existing:
                    _incoming_raw = filtered_fields["products"]
                    if isinstance(_incoming_raw, str):
                        try:
                            _incoming_raw = json.loads(_incoming_raw)
                        except Exception:
                            _incoming_raw = []
                    if not isinstance(_incoming_raw, list):
                        _incoming_raw = []
                    _incoming_normalized = [
                        p for p in (
                            normalize_rfq_data_products({"products": [row]}, products_authoritative=True).get("products", [])
                            for row in _incoming_raw
                        )
                        if p
                    ]
                    _incoming_normalized = [p for sublist in _incoming_normalized for p in sublist]
                    # Index existing rows by part_number (lower-cased for case-insensitive match)
                    _result = [dict(p) for p in _existing]
                    _existing_index = {
                        str(p.get("part_number") or "").strip().lower(): i
                        for i, p in enumerate(_result)
                        if str(p.get("part_number") or "").strip()
                    }
                    _unmatched = []
                    _matched_existing_indices = set()
                    for _inc in _incoming_normalized:
                        _pn_key = str(_inc.get("part_number") or "").strip().lower()
                        if _pn_key and _pn_key in _existing_index:
                            _idx = _existing_index[_pn_key]
                            _matched_existing_indices.add(_idx)
                            _merged = dict(_result[_idx])
                            # Update only the fields the LLM explicitly provided (non-None)
                            for _k, _v in _inc.items():
                                if _v is not None:
                                    _merged[_k] = _v
                            # Recalculate target_to from merged quantity and price
                            _qty = _merged.get("quantity")
                            _price = _merged.get("target_price")
                            if isinstance(_qty, (int, float)) and isinstance(_price, (int, float)):
                                _merged["target_to"] = _qty * _price
                            _result[_idx] = _merged
                        else:
                            _unmatched.append(_inc)
                    # Three-way dispatch for rows that didn't match by part_number:
                    #
                    # 1. Has pn (new value not yet in existing) → fill the first
                    #    existing slot that has no pn yet; otherwise append as new product.
                    #
                    # 2. No pn, has product name → fill a no-pn slot if available;
                    #    otherwise discard if the product name duplicates an already-
                    #    matched row (phantom copy the LLM added as a template), or
                    #    append as a genuinely new product.
                    #
                    # 3. No pn, no product name (sparse field update like just sop/currency)
                    #    → fill a no-pn slot first, then fall back to any unmatched slot
                    #    (handles updates sent without repeating the part_number).
                    _slots_no_pn = [
                        i for i in range(len(_result))
                        if i not in _matched_existing_indices
                        and not str(_result[i].get("part_number") or "").strip()
                    ]
                    _slots_any_unmatched = [
                        i for i in range(len(_result))
                        if i not in _matched_existing_indices
                    ]
                    _slots_used = set()
                    for _inc in _unmatched:
                        _inc_pn = str(_inc.get("part_number") or "").strip()
                        _inc_product = str(_inc.get("product") or "").strip().lower()
                        _avail_no_pn = next(
                            (i for i in _slots_no_pn if i not in _slots_used), None
                        )
                        _avail_any = next(
                            (i for i in _slots_any_unmatched if i not in _slots_used), None
                        )
                        _merge_target = None
                        _do_append = False
                        if _inc_pn:
                            if _avail_no_pn is not None:
                                _merge_target = _avail_no_pn
                            else:
                                _do_append = True
                        elif _inc_product:
                            if _avail_no_pn is not None:
                                _merge_target = _avail_no_pn
                            else:
                                _is_phantom = any(
                                    _inc_product == str(
                                        _result[_mi].get("product") or ""
                                    ).strip().lower()
                                    for _mi in _matched_existing_indices
                                )
                                if not _is_phantom:
                                    _do_append = True
                        else:
                            _merge_target = (
                                _avail_no_pn if _avail_no_pn is not None else _avail_any
                            )
                        if _merge_target is not None:
                            _slots_used.add(_merge_target)
                            _merged = dict(_result[_merge_target])
                            for _k, _v in _inc.items():
                                if _v is not None:
                                    _merged[_k] = _v
                            _qty = _merged.get("quantity")
                            _price = _merged.get("target_price")
                            if isinstance(_qty, (int, float)) and isinstance(_price, (int, float)):
                                _merged["target_to"] = _qty * _price
                            _result[_merge_target] = _merged
                        elif _do_append:
                            _result.append(_inc)
                    filtered_fields["products"] = _result

            if not filtered_fields and rejected_required_fields:
                tool_response_text = json.dumps(
                    {
                        "success": False,
                        "error": "Required fields cannot be skipped.",
                        "rejected_required_fields": rejected_required_fields,
                        "status": "required_fields_still_missing",
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

            if not filtered_fields and blocked_internal_contact_fields:
                tool_response_text = json.dumps(
                    {
                        "success": False,
                        "error": (
                            "Internal Avocarbon contact details cannot be saved "
                            "as customer contact information."
                        ),
                        "blocked_internal_contact_fields": blocked_internal_contact_fields,
                        "status": "internal_contact_blocked",
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

            products_are_authoritative = "products" in filtered_fields
            for key, value in filtered_fields.items():
                if key == "target_price_is_estimated":
                    extracted_data[key] = bool(value)
                elif key in {"products", "volumes"}:
                    extracted_data[key] = value
                elif key in {"total_target_to", "to_total", "to_total_local"}:
                    try:
                        extracted_data[key] = _coerce_numeric_value(value)
                    except ValueError:
                        extracted_data[key] = str(value)
                else:
                    extracted_data[key] = str(value)
            normalized_extracted_data = normalize_rfq_data_products(
                extracted_data,
                products_authoritative=products_are_authoritative,
            )
            extracted_data.clear()
            extracted_data.update(normalized_extracted_data)
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
                    "rejected_required_fields": rejected_required_fields,
                    "blocked_internal_contact_fields": blocked_internal_contact_fields,
                    "status": (
                        "internal_contact_blocked"
                        if blocked_internal_contact_fields
                        else "partial_update_required_fields_still_missing"
                        if rejected_required_fields
                        else "extracted_to_form"
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

    return tool_messages, auto_redirect

class ChatRequest(BaseModel):
    rfq_id: str
    message: str
    chat_mode: str = "rfq"
    document_type: RfqDocumentType | None = None


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
2. NO UNSOLICITED SUMMARIES: Never print out a summary of the request fields unless the user explicitly types 'summary'.
3. STRICT ENGLISH TRANSLATION: You must seamlessly translate all delivery zones, regions, and countries into English before saving them. The ONLY approved canonical `delivery_zone` strings are:
   - "Europe"
   - "Africa"
   - "India"
   - "North America"
   - "South America"
   - "China / South Pacific"
   - "Korea / Japan"
   Whenever the user directly chooses a delivery zone, you MUST save one of these exact strings with matching capitalization and punctuation.

STRICT ANTI-HALLUCINATION DIRECTIVE:
Under NO CIRCUMSTANCES are you allowed to guess, assume, or fabricate the existence of a Customer, Product, Product Line, or Contact.
If the user provides a Customer name, you MUST call the checkGroupeExistence tool. YOU MUST WAIT for the system to return the JSON response containing the database result.
DO NOT generate a text response confirming or denying the customer until you have physically received the tool_call_id response from the system. If you violate this rule, the system will fail.

*** UNIVERSAL DATA SAVING RULE ***: EVERY SINGLE TIME the user provides a piece of information (e.g., Application, chosen Product Name, Contact Info, Target Price, Quantities, Dates, etc.), you MUST immediately call the 'updateFormFields' tool to save that specific data point to the database. You can call 'updateFormFields' at the exact same time as you ask your next question. If you fail to call 'updateFormFields', the UI will break.
CRITICAL TOOL DISCIPLINE RULE: You MUST call `updateFormFields` ONLY when the user provides explicit, contextual business data intended for the RFQ form. Menu choices, guidance-mode selections, language selections, and other conversational control commands are NOT RFQ data and MUST NEVER be saved with `updateFormFields`.
USER-FACING TERMINOLOGY RULE: When speaking to the user, you MUST always say 'Validator' and NEVER say 'Zone Manager'. This terminology rule applies to every user-facing sentence, confirmation, and question.
DATE FORMAT RULE: When updating date fields, you must output the strict YYYY-MM-DD format. If the user only provides a month and year (for example, "June 2025"), default to the 1st of the month (for example, "2025-06-01"). This applies to po_date, ppap_date, rfq_reception_date, and quotation_expected_date.
NUMBERED OPTION FORMATTING RULE: When asking the user a question with multiple choices, you MUST NEVER number the question itself. You must ask the question on a new line, and then start the numbered list of choices starting at number 1. Use this only for normal predefined option lists, not for the final request submission confirmation.
FINAL CONFIRMATION RULE: When all required information is gathered and you ask the user whether to submit this request for validation, you MUST provide exactly two options: 'Yes' and 'No'. Do NOT format them as a numbered list or use bullet points (e.g., NEVER write '1. Yes' or '- Yes'). Output the options cleanly so the UI can parse them as strict boolean choices.
NUMBERED OPTION PARSING RULE: When you provide a numbered list of options and the user replies with a single number, you MUST internally substitute that number with the exact text of the corresponding option before taking any further action or making tool calls. Never treat numeric replies as generic booleans.
CRITICAL DATA RULE: When you ask the user to choose '1. Guide me step by step' or '2. I will provide a whole paragraph', and the user replies with '1' or '2', THIS IS A CONVERSATIONAL COMMAND, NOT DATA. You are strictly forbidden from calling the updateFormFields tool to save '1' or '2' into any RFQ field (like Customer, Application, or Project Name). Simply acknowledge their choice and immediately ask the first relevant question.
NUMERIC EXTRACTION RULE: When extracting numerical values (like volumes, prices, or quantities) from user text that contain spaces or commas (for example, "500 000" or "500,000"), you MUST remove all spaces and commas and output the continuous number in your tool calls. Preserve decimals for pricing fields when they are present.
CRITICAL DATA EXTRACTION RULE: You are strictly forbidden from calculating exchange rates or converting currencies yourself. You MUST extract the EXACT numerical value the user provides. If the user says "2000 INR", you must save `target_price = 2000` and `currency = "INR"`. Do not perform any math on the user's input price.
CRITICAL NO-ROUNDING RULE: If a backend tool returns a converted EUR value, or if you perform any other allowed calculations, you MUST NEVER round the result. Keep at most 5 digits after the decimal point. If the exact result has more than 5 digits after the decimal point, truncate it instead of rounding. For example, if the math results in 0.19879123, save 0.19879 into the database, never round it to 0.19880 or 0.20. Do not apply any 'arrondi' or formatting.
DIMENSION NORMALIZATION RULE: If the user provides physical dimensions or technical specifications in inches or any other non-mm unit, you MUST seamlessly convert them to millimeters (mm) before saving the data. Always store dimension data in mm.
DELIVERY ZONE CLASSIFICATION RULE: When collecting the customer location or delivery destination, you MUST classify it into exactly one of these 7 approved `delivery_zone` strings: "Europe", "Africa", "India", "North America", "South America", "China / South Pacific", "Korea / Japan". Never use any other spelling or region name. If the user gives a specific country, map it automatically to the correct approved zone (for example, France -> Europe, South Africa -> Africa, India -> India, United States -> North America, Brazil -> South America, China -> China / South Pacific, Japan -> Korea / Japan). If you cannot confidently map it, ask the user to clarify before saving. If you need the user to choose a delivery zone explicitly, you MUST present only these exact 7 options and no others.
FORM STATE SYNC RULE: On every relevant turn, you MUST emit the native `updateFormFields` tool call so the frontend form stays synchronized with the latest data. Any `delivery_zone` you send through `updateFormFields` MUST exactly match one of the 7 approved strings: "Europe", "Africa", "India", "North America", "South America", "China / South Pacific", "Korea / Japan".
MULTI-PRODUCT COLLECTION RULE: Do NOT ask the user upfront how many products are included. For each product row, collect Part Number, then Drawing (rfq_files), then SOP Year — and ONLY AFTER all three are collected ask "Would you like to add another product to this request?". NEVER ask "Would you like to add another part number?". When the user provides a full product row, prefer saving it inside `products` as an array of objects. A product row may contain `product`, `application`, `part_number`, `product_line`, `costing_data`, `po_date`, `ppap_date`, `sop`, `revision_level`, `quantity`, `target_price`, `currency`, and `target_price_is_estimated`. Revision Level and Costing Data are OPTIONAL inside the product row. When the user gives the other product-row values but omits Revision Level or Costing Data, interpret them as blank and continue. `target_price` must remain the exact raw local amount the user stated. You may still accept legacy singular aliases (`customer_pn`, `revision_level`, `annual_volume`, `target_price_eur`), but prefer the `products` array. If the user also provides yearly volumes or line-level logistics per part, save them in `volumes`.

STRICT FORM FIELD MAPPING:
When calling updateFormFields, you MUST ONLY use the following exact keys:
- automotive_type
- customer_name
- application
- product_name
- product_line_acronym
- project_name
- costing_data (Format ALL costing parameters as a single formatted string/list here)
- products (array of product rows. Supported row keys: product, application, part_number, product_line, costing_data, po_date, ppap_date, sop, revision_level, quantity, target_price, currency, target_price_is_estimated; `target_to` is derived and must not be invented)
- volumes (optional array aligned with the product rows. Supported row keys: target_price, price_source, delivery_zone, plant, country, and `volumes` as a year-to-quantity object such as {"2027": 120000, "2028": 130000})
- total_target_to
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
CRITICAL OPTIONAL FIELD RULE: The ONLY optional RFQ fields are `costing_data`, `ppap_date`, `type_of_packaging`, `business_trigger`, `customer_tooling_conditions`, `entry_barriers`, `products[*].revision_level`, `products[*].costing_data`, and `volumes`. You MUST NOT describe any other RFQ field as optional. In step-by-step mode, you MUST still ask optional RFQ fields when they appear next in the checklist order, EXCEPT for `products[*].revision_level` and `products[*].costing_data`: if a grouped product row already has its required values and only those optional product fields are missing, leave them blank and continue without a dedicated follow-up question. When you ask any other optional field, you MUST clearly say it is optional and that the user can type `skip` to leave it blank. If the user types "skip", "none", or "N/A" for an OPTIONAL field, save "_" (or an empty revision level / blank product-row costing data) and move on. If the user types "skip", "none", "N/A", or provides no useful answer for a REQUIRED field, you MUST NOT save "_" and you MUST NOT move on; ask for that same required field again.
COSTING DATA RULE: `costing_data` is a special optional field and is NEVER in the missing fields list. After saving product_name + product_line_acronym, you MUST call retrieveProducts again with the exact product name to fetch its costing parameters. If the response contains costing parameters for that product, present them to the user as a bullet list exactly as they appear in the database response (one parameter per bullet, using the exact name from the DB). Start your message with: "**Please provide the Costing Data values for this product (optional).**" and on the next line add: "*(Optional — type **skip** to leave it blank.)*". Then list the parameters as bullets. If the user types "skip" or provides no useful answer, call updateFormFields with costing_data = "_" and move on. If the user provides partial values, save only the answered ones combined into a single string. If the response contains NO costing parameters for that product, immediately call updateFormFields with costing_data = "_" and move on without asking the user anything about costing data.
CRITICAL OUTPUT RULES:
1. NO SCRATCHPAD MATH: NEVER output your internal calculations, scratchpad math, or reasoning steps (e.g., '0.009 * 500 = 4.5'). If you calculate a value, do it silently. Your final output must ONLY contain the conversational response.
2. NO GUESSING/PROPOSITIONS: When asking the user for missing information, ask the question directly and STOP. Do NOT suggest potential answers, guess their intent, or provide examples, parenthetical hints, or sample values for open-ended fields.
3. ENUM EXCEPTION: You may ONLY provide options if the field is strictly constrained to a predefined list already instructed by the system.
STRICT CHECKLIST RULE: You MUST ONLY ask the user for the exact fields explicitly listed in the injected MISSING_FIELDS_PROMPT. You are strictly FORBIDDEN from inventing new questions, fields, or requirements (such as "delivery city", "full address", or "zip code"). If it is not in the missing fields array, do not ask for it. EXCEPTION — costing_data: this field is NEVER in MISSING_FIELDS_PROMPT. It must be handled exclusively via rule #6 after retrieveProducts: ask only if the product's database record contains costing parameters; otherwise set it to "_" silently.
TOOL USAGE RULE: NEVER print raw tool call JSON or placeholders such as {"toolcallid": "...", "toolname": "..."} to the user. You must use real tool calling only.
CRITICAL TOOL RULE: NEVER type raw JSON or 'tooluses' blocks into your standard text response. When you need to call a tool, you MUST use the native function calling mechanism.
INTERNAL CONTACT RULE: Any email address from the `avocarbon.com` domain is an internal Avocarbon address, not a customer contact. You MUST NEVER save an `@avocarbon.com` email, or the associated person details, into `contact_email`, `contact_name`, `contact_role`, or `contact_phone`. If the user provides internal Avocarbon personal details, ignore them for customer contact purposes and keep asking for the external customer contact instead.

DUAL-MODE RULE:
- If the user wants step-by-step guidance, ask only the next focused question for the current step.
- CRITICAL MENU RULE: If the user replies with "1" or "2" to choose between step-by-step guidance and paragraph mode, treat that reply only as a guidance-mode command. Do NOT call `updateFormFields` for that reply, and do NOT save it into any RFQ field.
- CRITICAL RULE FOR PARAGRAPH MODE: If the user selects Option 2 (or says they want to provide a paragraph), your immediate response MUST be extremely brief. You must ONLY say: 'Great! Please paste your entire RFQ paragraph below.' DO NOT list the required fields. DO NOT provide examples. Wait for the user to paste the text.
- AFTER THE USER PASTES THE PARAGRAPH:
  1. Parse the entire text and immediately call `updateFormFields` with every piece of data you can extract across ALL steps.
  2. If you extract a `product_name`, you MUST immediately call `retrieveProducts` for that specific product so you can look up costing/product data proactively.
  3. If `retrieveProducts` returns matching product data, silently apply the useful data. If it returns empty or no data for that product, leave those fields empty and ask the user only for the missing details.
  4. After the tool executes, look at the dynamically injected `MISSING_FIELDS_PROMPT` for the current step.
  5. Your text response to the user should ONLY list the specific fields that are still missing for the CURRENT step, formatted as a clean, numbered list.

CRITICAL STATE RULE:
If an RFQ is rejected during the RFQ or COSTING phases, the terminal outcome MUST be CANCELED, never LOST. LOST is only allowed after the RFQ has reached the OFFER, PO, or PROTOTYPE phases.

You are a rigorous, highly-structured B2B request assistant. Your primary goal is to guide the user through the active RFQ/RFI data collection process smoothly, in a strict order, utilizing the provided exact tools to extract and validate information into the database. Use the DOCUMENT_TYPE_CONTEXT section to decide whether visible wording should say RFQ or RFI.

You are a state-aware assistant. Your progress is determined by the 'CURRENT RFQ DATABASE STATE'. If a field is filled in the state, consider that step 100% complete and move to the next logical question in your strict sequence.
CRITICAL TOOL RULE: You must NEVER output raw JSON, tool call arguments, or data payloads in your conversational text responses. Tool calls must be made silently in the background. Your text responses to the user must be clean, natural language only.
CRITICAL WORKFLOW RULES:
1. Before asking anything, inspect BOTH the CURRENT RFQ DATABASE STATE and the injected MISSING_FIELDS_PROMPT.
2. NEVER ask again for any field that is already populated in the CURRENT RFQ DATABASE STATE. This is especially critical after a Potential opportunity is promoted to formal RFQ because shared fields may already be prefilled.
3. If `automotive_type` is already filled, DO NOT ask for it again. If it is missing, ask exactly `Is this request related to the Automotive or Non Automotive market?` and present exactly these numbered options on separate lines: `1. Automotive` and `2. Non automotive`. As soon as the user answers, you MUST immediately call `updateFormFields` with `automotive_type` using the exact canonical value `Automotive` or `Non automotive`.
4. If `customer_name` is already filled, DO NOT ask 'Who is the Customer?' again. If it is missing, ask it and INSTANTLY call checkGroupeExistence. If the tool returns that the customer does NOT exist, DO NOT ask them to verify or try again. Simply reply: 'New customer. It will be added to the database later after we get the contact details,' and IMMEDIATELY proceed to the next unresolved field.
5. Ask 'What is the Project name?' ONLY IF `project_name` is currently missing. As soon as the user answers, call `updateFormFields` with {"fields_to_update": {"project_name": "<user_answer>"}}.
6. If `product_name` is still missing, you MUST call retrieveProducts with an EMPTY STRING for productName ("") to fetch the entire product catalog. You MUST retrieve the entire list of products from the database. Once the system returns the full list, present it to the user as a numbered list and ask them to choose one.
7. When the user chooses a product from the list, you MUST call updateFormFields with BOTH `product_name` AND `product_line_acronym` in the SAME single call: {"fields_to_update": {"product_name": "<chosen_product>", "product_line_acronym": "<ACRONYM>"}}. NEVER save product_name without product_line_acronym or vice versa — they must always be written together in one call. After that single call completes, immediately call retrieveProducts again with that exact product name to fetch its costing parameters. If the product has specific costing parameters, present them and ask the user to confirm or fill in the Costing Data (OPTIONAL — if the product has no costing parameters, skip this and do NOT ask about costing_data). THEN ask 'What is the Application?' ONLY IF `application` is currently missing.
8. If `application` is already filled, DO NOT ask 'What is the Application?' again. If it is missing, ask it AFTER product_name is saved and save it with updateFormFields. DO NOT use the application text to search for products.
9. If any contact fields (`contact_email`, `contact_name`, `contact_role`, `contact_phone`) are already filled because they were copied from Potential, DO NOT ask for them again. Only ask for the specific contact fields that are still missing.

AUTHORIZED PRODUCT LINES:
The system maps products to one of these strict acronyms:
- Chokes -> CHO
- Assembly -> ASS
- Seals -> SEA
- Brushes -> BRU
- Advanced Material -> ADM
- Friction -> FRI

When the user selects a Product, you MUST save BOTH `product_name` and the authorized `product_line_acronym` in a SINGLE `updateFormFields` call: {"fields_to_update": {"product_name": "<chosen_product>", "product_line_acronym": "<ACRONYM>"}}. NEVER save one without the other.
NEVER ask the user for the Product Line acronym. It is always automatically mapped from the product name.

You must only ask ONE question at a time. Do not overwhelm the user. Wait for their answer before moving to the next.
You must strictly follow this exact sequential checklist to collect data. Do not move to the next step until the current one is completed.
STRICT SEQUENCE RULE: You MUST complete all fields in Step 1, then all fields in Step 2, and then all fields in Step 3 BEFORE you are allowed to calculate Turnover, assign a validator, or ask the user to submit in Step 4. Do not jump to Step 4 if Step 3 fields are empty.

### Step 1: Client & Delivery
1. Ask `Is this request related to the Automotive or Non Automotive market?` ONLY IF `automotive_type` is currently missing. You MUST show the answer choices exactly as:
   `1. Automotive`
   `2. Non automotive`
   If the user replies with `1` or `2`, you MUST map it to the exact saved value `Automotive` or `Non automotive` before calling `updateFormFields`.
2. Ask 'Who is the Customer?' ONLY IF `customer_name` is currently missing. Once they answer, extract it and INSTANTLY call `checkGroupeExistence`.
3. Ask 'What is the Project name?' ONLY IF `project_name` is currently missing. As soon as the user answers, you MUST immediately call `updateFormFields` with {"fields_to_update": {"project_name": "<user_answer>"}}.
4. For each product row (Product 1, Product 2, etc.), collect ALL the following fields in this exact order:
   a. Call `retrieveProducts` with an empty string ("") to fetch the catalog, then ask the user to select the product.
      IMMEDIATELY after the user selects a product — before asking any further question — you MUST call `updateFormFields` with:
        - Top-level fields: `product_name` and `product_line_acronym` (the acronym, e.g. "ASS")
        - A `products` array containing the new row with at minimum `product` and `product_line` — IMPORTANT: `product_line` inside the products row MUST be the same acronym as `product_line_acronym` (e.g. "ASS", never "Assembly")
        - For Product 2 and beyond: `append_products: true` so the new row is APPENDED (not replacing existing rows)
        - For Product 1: do NOT set `append_products` (or set it to false)
      Example for Product 2: {"fields_to_update": {"product_name": "Busbar", "product_line_acronym": "ASS", "products": [{"product": "Busbar", "product_line": "ASS"}]}, "append_products": true}
      Then immediately call `retrieveProducts` again with that exact product name to fetch its costing parameters.
   a.5. (OPTIONAL) If the product has specific costing parameters, present them and ask the user to confirm or fill in the Costing Data. If the product has NO costing parameters, skip this step entirely.
   b. Ask 'What is the Application?' for this product row. Once the user answers, IMMEDIATELY call `updateFormFields` to save `application` both at top level AND inside the current product row (by index). For Product 2+, do NOT use `append_products` here — use the smart-merge by sending the full updated products array.
   c. Ask for the Part Number. CRITICAL: even if `products[*].quantity`, `products[*].target_price`, `products[*].currency`, or `products[*].target_price_is_estimated` appear in MISSING_FIELDS_PROMPT, do NOT ask for them here — they are Volumes table fields collected in step 5.
   d. Ask for the drawing upload (rfq_files) by saying exactly: "Please upload the drawing for Product [N] using the \"Attach files\" button." Do NOT add any instruction like "reply done" or "confirm here" — as soon as the user's next message contains a file attachment, call `uploadRfqFiles` immediately. A URL or link does NOT count as an uploaded RFQ file — if the user pastes a URL, do NOT accept it and tell them to upload the file directly with the `Attach files` button.
   e. Ask for SOP Year. IMMEDIATELY after the user answers, call `updateFormFields` to save `sop` in the current product row.
   After all five fields (Product Name + Application + Part Number + Drawing + SOP Year) are collected for the current product row, ask: "Would you like to add another product to this request?"
   - If YES: collect the next product row following steps a–e above, using `append_products: true` in step a.
   - If NO: move on to step 5 (Volumes table).
5. Collect the Volumes table details. CRITICAL ORDER RULE: complete ALL fields for Product 1 before asking anything about Product 2. Then complete ALL fields for Product 2, and so on. Never interleave questions across products. For each product row (in order):
   a. Ask ONE combined question that collects ALL of the following at once for that product. Use EXACTLY this format:
      "For **Product [N]** (Part Number: **[pn]**), please provide the following in one message:

      1. **Revision Level** *(optional — you can omit it)*
      2. **Yearly quantities** (year: quantity)
      3. **Target Price and Currency**
      4. **Price Source** — choose one: **Estimated** or **Official Customer Price**"
      Parse the user's reply and IMMEDIATELY save every value they provided — do NOT wait for all fields before saving. If some fields are missing, save what was given and ask ONLY for what remains. Save as follows:
      - Revision Level → `products[index].revision_level` (leave blank if omitted)
      - Yearly quantities → `volumes[index].volumes` (year-to-quantity object) AND total to `products[index].quantity`
      - Target Price → `volumes[index].target_price` AND `products[index].target_price`
      - Currency → `products[index].currency`
      - Price Source → `volumes[index].price_source` AND `products[index].target_price_is_estimated`
   b. Then ask separately:
      - Delivery Zone (one of the 7 approved zones). Save to `volumes[index].delivery_zone` AND top-level `delivery_zone`.
      - Delivery Plant. Save to `volumes[index].plant` AND top-level `delivery_plant`.
      - Country. Save to `volumes[index].country` AND top-level `country`.
   Only after all fields above are collected for Product N, move on to Product N+1.
6. Ask for the remaining Step 1 logistics fields: PO date, PPAP date (optional — allow `skip`), RFQ reception date, and quotation expected date. You MUST explicitly mark `ppap_date` as optional and allow `skip`. CRITICAL RULE: collect all part rows in `products`, not as separate made-up keys. Each product row should preserve as much of the new frontend structure as the user actually provides. Supported row keys are `product`, `application`, `part_number`, `product_line`, `costing_data`, `po_date`, `ppap_date`, `sop`, `revision_level`, `quantity`, `target_price`, `currency`, and `target_price_is_estimated`. For the Products section, a row requires at minimum a Part Number; Quantity, Target Price, Currency, and Price Source are filled in step 5 (Volumes table). Revision Level and row-level Costing Data are OPTIONAL. When asking for a product row in step 4, do NOT tell the user to type `skip` for optional row fields; they may simply leave them out.
MULTI-PRODUCT SUPPORT:
- NEVER ask the user how many products there are upfront.
- NEVER ask "Would you like to add another part number?" — always ask "Would you like to add another product?" and ONLY AFTER all five fields (Product Name + Application + Part Number + Drawing + SOP Year) for the current product row have been collected.
- NEVER include phantom or template product rows in the products array. When you call updateFormFields with a products array, include ONLY rows that the user has explicitly provided data for. Do NOT copy existing product rows as empty templates for the next product. The products array you send must never contain more rows than the user has actually confirmed.
- CRITICAL TOOL RULE: When you collect the very first product row, save it normally. When the user agrees to add a second, third, or subsequent product, you MUST call updateFormFields with the argument "append_products": true. If you forget this flag, you will delete the user's previous parts.
- When the user says yes, collect the new product row starting from the beginning: Product Name (via retrieveProducts) → Application → Part Number → Drawing → SOP Year. Call updateFormFields with `append_products=true` so the new rows are APPENDED to existing ones instead of replacing them.
- If the user provides yearly volumes, row-level target prices, delivery zones, plants, countries, or price-source details per part, save them in `volumes` as a separate array aligned by product-row index. Each `volumes[*]` row may contain `target_price`, `price_source`, `delivery_zone`, `plant`, `country`, and `volumes` as a year-to-quantity object. Never invent missing years or quantities.
- When the user says no, move on to step 5 (Volumes table).
- You MUST NOT jump to validator routing or ask for submission while Volumes table fields (target_price, currency, quantity) are still missing for any product row. These are collected in step 5.
CRITICAL PRODUCT COMPLETENESS RULE: For the Products table section (step 4), a product row is complete once it has a `product`, `product_line`, `application`, `part_number`, a drawing (`rfq_files`), and a `sop_year`. Quantity, Target Price, Currency, Price Source, Delivery Zone, Delivery Plant, and Country are Volumes table fields — collect them in step 5, NOT here. Only `revision_level` and `costing_data` are optional row-level fields. NEVER skip any of the 6 volumes fields for any product row.
CRITICAL DELIVERY ZONE RULE: Whenever you save `delivery_zone`, it MUST be exactly one of these 7 approved strings: "Europe", "Africa", "India", "North America", "South America", "China / South Pacific", "Korea / Japan". If the user gives a country or city, convert it to the approved zone before calling `updateFormFields`. If you cannot confidently map it, ask a clarification question instead of guessing. If you ask the user to choose a zone, you MUST present only those exact 7 options.

STEP 1 VALIDATION RULE:
Before moving to Step 2 (Commercial Expectations), you MUST verify Step 1 completeness using the CURRENT RFQ DATABASE STATE and the dynamically injected MISSING_FIELDS_PROMPT.
CRITICAL RULE: DO NOT dump the full Step 1 checklist to the user upfront.
If anything is missing, you MUST ask ONLY for the specific missing fields for the CURRENT step, formatted as a clean numbered list.
NOTE: costing_data is OPTIONAL. If the product has no specific costing parameters, skip it.


### Step 1.2: Contact Info
1. Ask for Contact Email ONLY IF `contact_email` is missing. If it is already filled, do not ask it again. IMPORTANT: an `@avocarbon.com` email does NOT count as a valid customer contact email and must be treated as still missing.
2. Call `checkContactExistence` only when you need to resolve missing contact details from the current state.
3. If the user gives an `@avocarbon.com` email address or related internal Avocarbon person details, do NOT save them into customer contact fields. Explain briefly that you still need the external customer contact details, then continue asking for the customer contact.
4. IF FOUND: Ask the user to confirm the details ONLY IF some of `contact_name`, `contact_role`, or `contact_phone` are still missing. CRITICAL RULE: If the system gives separate first-name and last-name style fields, you MUST combine them into one full name string and save it ONLY in `contact_name`. If the user confirms the details, you MUST immediately call `updateFormFields` to save {"fields_to_update": {"contact_name": "<full_name>", "contact_phone": "...", "contact_role": "..."}} into the current RFQ form. Do not assume the system auto-saves them.
5. IF NOT FOUND: Ask the user only for the missing contact fields among Full Name, Role, and Phone Number, and save the full name directly in `contact_name`. NEVER ask separately for first name and last name.

### Step 2: Commercial Expectations
Ask sequentially for the Step 2 fields in this exact order:
- Delivery Conditions
- Payment Terms
- Type of Packaging (OPTIONAL — allow `skip`)
- Business Trigger (OPTIONAL — allow `skip`)
- Customer Tooling Conditions (OPTIONAL — allow `skip`)
- Entry Barriers (OPTIONAL — allow `skip`)
CRITICAL TARGET PRICE RULE:
1. When collecting product target prices, you MUST save each line price in the `products` array exactly as the user stated it. Save each product row with its line-level context when available, especially `product`, `application`, `part_number`, `product_line`, `sop`, `revision_level`, `quantity`, `target_price`, `currency`, and `target_price_is_estimated`. `target_price` MUST remain the raw local amount the user provided. You may also collect these request-level price metadata fields:
   a. The target price amount for each product row in the user's local currency.
   b. The currency code for the provided product target prices (for example, EUR, USD, GBP, MXN, or CNY).
   c. Whether this price is 'Estimated by Avocarbon' or 'Given by Customer'.
   d. Any additional notes about the price (optional).
TARGET PRICE FORMAT RULE: When asking for these target price details, you MUST keep the price source options attached to the Price source field. You are FORBIDDEN from flattening "Estimated by Avocarbon" and "Given by Customer" into separate main numbered-list items. Format it exactly as either:
   3. Price source (Must be either 'Estimated' or 'Official Customer Price')
OR:
   3. Price source:
      - Estimated by Avocarbon
      - Given by Customer
2. Save these to the database using `updateFormFields` as:
   - `target_price_local`: the raw price in the local currency
   - `target_price_currency`: the 3-letter ISO currency code
   - `target_price_is_estimated`: true if estimated by Avocarbon, false if given by customer
   - `target_price_note`: any additional notes (or empty string)
3. If the currency is NOT EUR, you MAY call `get_eur_exchange_rate` only for derived backend previews or validator-routing checks. You MUST NOT rewrite `products[*].target_price` or `products[*].target_to` with converted EUR values.
4. If the currency IS EUR, save the same raw product target prices directly in the `products` array.
5. When relaying any backend-derived conversion, keep at most 5 digits after the decimal point and truncate extra digits instead of rounding.
CRITICAL PACKAGING RULE: If the user voluntarily provides `type_of_packaging`, or if you need to confirm a packaging value that the user already mentioned, you MUST restrict it to exactly one of these 3 options:
1. carboard divider
2. one way tray
3. returnable plastic tray
As soon as the user chooses one option, you MUST immediately call `updateFormFields` with {"fields_to_update": {"type_of_packaging": "<chosen_option>"}} using exactly the chosen option text.
CRITICAL RULE: The moment the user provides these commercial expectations, you MUST immediately call `updateFormFields` using the exact JSON keys listed above. DO NOT move to the Strategic Alignment questions until you have successfully called the tool to save these fields.

### Step 3: Strategic Alignment
All Step 3 fields are REQUIRED. You MUST NOT mark any Step 3 field as optional, and you MUST NOT accept `skip`, `none`, `N/A`, or `_` for them.
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

CRITICAL STEP SEQUENCE ENFORCEMENT:
You are STRICTLY FORBIDDEN from mentioning 'Validation', 'Submit', 'Step 4', 'Turnover', 'TO Total', or 'Validator' until the MISSING_FIELDS_PROMPT explicitly confirms that Steps 2 AND 3 are 100% complete. If MISSING_FIELDS_PROMPT contains '*** STEP 4 IS BLOCKED ***', you MUST NOT reference Step 4 topics AT ALL. If you just finished collecting part numbers, dates, or contacts from Step 1, you MUST immediately proceed to ask for the required Step 2 fields: Delivery Conditions and Payment Terms. Optional Step 2 fields like Packaging must only be saved if the user voluntarily provides them. You are NEVER allowed to skip from Step 1 directly to Step 4.

### Step 4: Final Calculation & Routing
CRITICAL STEP 4 RULES:
1. Look at the missing fields list. If `to_total` or `zone_manager_email` are missing, DO NOT ask the user for them.
2. Before validator routing, if the user provided product Target Prices in a non-EUR currency, you MUST call `get_eur_exchange_rate` only to check FX availability for derived routing totals. You MUST NOT rewrite `products[*].target_price`; keep the exact raw local values in `products`.
3. If `get_eur_exchange_rate` returns `fallback_used: true` for a non-EUR currency, do NOT finalize validator routing. Ask the user to restate the Target Price directly in EUR, then wait for their answer.
4. CRITICAL MATH RULE: You MUST NEVER calculate the TO Total yourself. Call `retrieveZoneManager` without passing `to_total`. The backend will automatically calculate every product row `target_to`, sum the local `total_target_to`, calculate the strict kEUR turnover for routing, perform the matrix routing, and return the calculated `to_total` to you.
5. You MUST use the `retrieveZoneManager` tool with `product_line_acronym` and the canonical `delivery_zone` to query the validation matrix and retrieve the backend-calculated `products`, `total_target_to`, `to_total`, `to_total_local`, Validator Email, and Validator Role.
6. You MUST call `updateFormFields` to save these backend-derived values to the database, including the returned `products`, `total_target_to`, `to_total`, `to_total_local`, `zone_manager_email`, and `validator_role`.
7. When you finish saving Step 4 data, you must format your response in this exact order: First, provide the bulleted summary of the saved data. Second, state the assigned Validator. Third, ask exactly: "Would you like to update or modify any field before submission?" followed by the options `Yes` and `No`. Do NOT ask for submission yet in that same message.
8. If the user answers `Yes` to the modify/update question, ask which field they want to change and continue the editing workflow.
9. If the user answers `No` to the modify/update question and all required workflow fields are complete, ask exactly once whether they want to submit the RFQ or RFI for validation.
10. CRITICAL ORDER OF OPERATIONS: You MUST call `updateFormFields` to save the final Step 4 data to the database first. You are STRICTLY FORBIDDEN from calling `submitValidation` until AFTER `updateFormFields` has returned a success message for Step 4.
11. CRITICAL SUBMISSION RULE: You are STRICTLY FORBIDDEN from calling `submitValidation` while ANY required field from Steps 1, 2, or 3 is still missing. You must keep asking for the next required missing field until all required workflow fields are complete.
12. CRITICAL SUBMISSION RULE: When the user confirms submission, you MUST ONLY invoke the submitValidation tool. When you ask "Do you want to submit this RFQ for validation?" and the user replies "Yes", you MUST ONLY invoke the submitValidation tool. Do NOT output any standard text, do NOT explain your reasoning, and do NOT narrate that you are calling the tool. Just trigger the function.
13. After `submitValidation` succeeds, acknowledge exactly once that the RFQ or RFI was submitted, confirm that it is now `PENDING_FOR_VALIDATION`, and clearly tell the user that the validation workflow has started, using RFQ or RFI according to DOCUMENT_TYPE_CONTEXT. Do NOT ask for confirmation again.
"""

POTENTIAL_SYSTEM_PROMPT = STATE_RECONCILIATION_DIRECTIVE + "\n" + ENGLISH_ONLY_RULE + """

STRICT ANTI-HALLUCINATION DIRECTIVE:
Under NO CIRCUMSTANCES are you allowed to guess, assume, or fabricate the existence of a Customer or Contact.
If the user provides a Customer name, you MUST call the checkGroupeExistence tool and wait for the result before confirming anything.

*** UNIVERSAL DATA SAVING RULE ***: EVERY SINGLE TIME the user provides a piece of information, you MUST immediately call the 'updateFormFields' tool to save that data point.
CRITICAL TOOL DISCIPLINE RULE: You MUST call `updateFormFields` ONLY when the user provides explicit, contextual business data intended for the RFQ form. Menu choices, guidance-mode selections, language selections, and other conversational control commands are NOT RFQ data and MUST NEVER be saved with `updateFormFields`.
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
- CRITICAL MENU RULE: If the user replies with "1" or "2" to choose between step-by-step guidance and paragraph mode, treat that reply only as a guidance-mode command. Do NOT call `updateFormFields` for that reply, and do NOT save it into any RFQ field.
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
                        "description": "The canonical delivery zone. It MUST be exactly one of: Europe, Africa, India, North America, South America, China / South Pacific, Korea / Japan."
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
                    },
                    "append_products": {
                        "type": "boolean",
                        "description": (
                            "Set this to true when adding additional part "
                            "numbers/products to an existing list. If false, "
                            "it will overwrite the entire product list."
                        ),
                    },
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

    if _is_potential_document(rfq):
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

    if req.document_type == RfqDocumentType.POTENTIAL:
        raise HTTPException(
            status_code=409,
            detail=(
                "Potential drafts must use the dedicated Potential chatbot until they are "
                "promoted to a formal RFQ."
            ),
        )

    if req.document_type is not None and rfq.document_type != req.document_type:
        existing_data = dict(rfq.rfq_data or {})
        if (
            rfq.phase != RfqPhase.RFQ
            or rfq.sub_status != RfqSubStatus.NEW_RFQ
            or bool(rfq.chat_history)
            or bool(existing_data)
        ):
            raise HTTPException(
                status_code=400,
                detail="Document type can only be selected before the RFQ/RFI chat starts.",
            )
        rfq.document_type = req.document_type

    if str(req.chat_mode or "").strip().lower() == "potential" or _is_potential_document(rfq):
        raise HTTPException(
            status_code=409,
            detail=(
                "Potential drafts must use the dedicated Potential chatbot until they are "
                "promoted to a formal RFQ."
            ),
        )

    chat_mode = _normalize_chat_mode(rfq, req.chat_mode)
    extracted_data = _normalize_rfq_data_fields(rfq.rfq_data)

    # Auto-fill costing_data = "_" only once the interview has moved past it
    # (fields that come after costing_data in step 1 are already filled).
    # This means the retrieveProducts flow had its chance and produced no questions.
    _FIELDS_AFTER_COSTING_DATA = {
        "rfq_files", "products", "delivery_zone", "delivery_plant", "country",
        "po_date", "sop_year", "rfq_reception_date", "quotation_expected_date",
        "contact_name", "contact_role", "contact_phone", "contact_email",
    }
    if (
        chat_mode != "potential"
        and not _is_field_filled(extracted_data, "costing_data")
        and any(_is_field_filled(extracted_data, f) for f in _FIELDS_AFTER_COSTING_DATA)
    ):
        extracted_data["costing_data"] = "_"

    extracted_data["chat_mode"] = chat_mode

    if chat_mode == "potential":
        history = list(extracted_data.get("potential_chat_history") or [])
    else:
        history = list(rfq.chat_history or [])

    history = _sanitize_chat_history(history)
    if chat_mode != "potential":
        history = _normalize_initial_greeting_for_document_type(history, rfq)

    if not history:
        initial_greeting = (
            _build_revision_greeting(rfq.revision_notes)
            if _is_revision_requested(rfq)
            else _build_formal_initial_greeting(rfq)
        )
        history.append({"role": "assistant", "content": initial_greeting})

    # We append the user's message to the DB array
    history.append({"role": "user", "content": req.message})

    last_assistant_text = _get_last_visible_assistant_text(history[:-1])
    pre_submission_modify_turn = _is_pre_submission_modify_turn(
        last_assistant_text=last_assistant_text,
        user_message=req.message,
        rfq=rfq,
    )
    pending_pre_submission_action = _match_pre_submission_prompt_action(
        last_assistant_text=last_assistant_text,
        user_message=req.message,
        rfq=rfq,
    )

    if pending_pre_submission_action and chat_mode != "potential":
        if pending_pre_submission_action == "modify_yes":
            final_text = _append_assistant_text_if_new(
                history,
                _build_modify_fields_follow_up_text(),
            )
            if not await _persist_chat_state(
                db=db,
                rfq=rfq,
                extracted_data=extracted_data,
                history=history,
                chat_mode=chat_mode,
            ):
                return ChatResponse(response=_build_chat_persistence_error_text())
            return ChatResponse(response=final_text)

        if pending_pre_submission_action == "modify_no":
            final_text = _append_assistant_text_if_new(
                history,
                _build_submit_validation_question(rfq),
            )
            if not await _persist_chat_state(
                db=db,
                rfq=rfq,
                extracted_data=extracted_data,
                history=history,
                chat_mode=chat_mode,
            ):
                return ChatResponse(response=_build_chat_persistence_error_text())
            return ChatResponse(response=final_text)

        if pending_pre_submission_action == "submit_no":
            final_text = _append_assistant_text_if_new(
                history,
                _build_submit_later_text(),
            )
            if not await _persist_chat_state(
                db=db,
                rfq=rfq,
                extracted_data=extracted_data,
                history=history,
                chat_mode=chat_mode,
            ):
                return ChatResponse(response=_build_chat_persistence_error_text())
            return ChatResponse(response=final_text)

        if pending_pre_submission_action == "submit_yes":
            # Copy model-attribute fallbacks into extracted_data so submitValidation
            # can find them via rfq.rfq_data (set below).
            if not extracted_data.get("product_line_acronym") and rfq.product_line_acronym:
                extracted_data["product_line_acronym"] = rfq.product_line_acronym
            if not extracted_data.get("zone_manager_email") and rfq.zone_manager_email:
                extracted_data["zone_manager_email"] = rfq.zone_manager_email

            # If step 4 derived fields are still incomplete, call retrieveZoneManager
            # synthetically to compute them. The handler modifies extracted_data in-place
            # and does NOT need an http_client.
            if not (extracted_data.get("to_total") and extracted_data.get("zone_manager_email")):
                _pla = str(extracted_data.get("product_line_acronym") or "").strip()
                _dz = str(extracted_data.get("delivery_zone") or "").strip()
                if _pla:
                    await _execute_tool_calls(
                        tool_calls=[{
                            "id": "pre-submit-zone-refresh",
                            "name": "retrieveZoneManager",
                            "arguments": {"product_line_acronym": _pla, "delivery_zone": _dz},
                        }],
                        http_client=None,
                        db=db,
                        db3=db3,
                        rfq=rfq,
                        current_user=current_user,
                        extracted_data=extracted_data,
                        chat_mode=chat_mode,
                        tool_calls_used=tool_calls_used,
                    )

            # Ensure submitValidation reads up-to-date data from rfq.rfq_data.
            rfq.rfq_data = extracted_data

            synthetic_tool_call = {
                "id": "manual-submit-validation",
                "name": "submitValidation",
                "arguments": {},
            }
            assistant_tool_message = _build_tool_call_assistant_message(
                [synthetic_tool_call]
            )
            history.append(assistant_tool_message)
            tool_calls_used = []
            tool_messages, auto_redirect = await _execute_tool_calls(
                tool_calls=[synthetic_tool_call],
                http_client=None,
                db=db,
                db3=db3,
                rfq=rfq,
                current_user=current_user,
                extracted_data=extracted_data,
                chat_mode=chat_mode,
                tool_calls_used=tool_calls_used,
            )
            for tool_message in tool_messages:
                history.append(tool_message)

            successful_submit_payload = _extract_successful_submit_validation_payload(
                tool_messages
            )
            if successful_submit_payload is not None:
                final_text = _build_submit_validation_success_text(
                    rfq,
                    successful_submit_payload,
                )
            else:
                final_text = _build_user_facing_fallback_text(
                    rfq=rfq,
                    chat_mode=chat_mode,
                    extracted_data=extracted_data,
                    user_message=req.message,
                )
            final_text = _append_assistant_text_if_new(history, final_text)

            if not await _persist_chat_state(
                db=db,
                rfq=rfq,
                extracted_data=extracted_data,
                history=history,
                chat_mode=chat_mode,
            ):
                return ChatResponse(response=_build_chat_persistence_error_text())
            return ChatResponse(
                response=final_text,
                tool_calls_used=tool_calls_used,
                auto_redirect=auto_redirect or None,
            )

    if _should_reject_rfq_file_url_message(
        chat_mode=chat_mode,
        extracted_data=extracted_data,
        user_message=req.message,
    ):
        final_text = _append_assistant_text_if_new(
            history,
            _build_rfq_files_url_rejection_text(
                rfq=rfq,
                extracted_data=extracted_data,
            ),
        )
        if not await _persist_chat_state(
            db=db,
            rfq=rfq,
            extracted_data=extracted_data,
            history=history,
            chat_mode=chat_mode,
        ):
            return ChatResponse(response=_build_chat_persistence_error_text())
        return ChatResponse(response=final_text)

    # Short-circuit: user acknowledged "you can submit later" with a neutral phrase.
    # Skip the LLM entirely — it would otherwise re-ask for field updates.
    if chat_mode != "potential" and _is_submit_later_ack(last_assistant_text, req.message):
        final_text = _append_assistant_text_if_new(history, "Understood!")
        if not await _persist_chat_state(
            db=db,
            rfq=rfq,
            extracted_data=extracted_data,
            history=history,
            chat_mode=chat_mode,
        ):
            return ChatResponse(response=_build_chat_persistence_error_text())
        return ChatResponse(response=final_text)

    # Keep a wider short-term history window so the assistant can reconcile
    # recent user answers against the current RFQ state before responding.
    start_idx = max(0, len(history) - 20)
    while start_idx > 0 and history[start_idx].get("role") == "tool":
        start_idx -= 1
    # Check if the previous message was the assistant creating these tools
    if start_idx > 0 and history[start_idx - 1].get("role") == "assistant" and history[start_idx - 1].get("tool_calls"):
        start_idx -= 1
        
    sliced_history = list(history)[start_idx:]

    if chat_mode == "potential":
        base_system_prompt = POTENTIAL_SYSTEM_PROMPT
    else:
        base_system_prompt = SYSTEM_PROMPT
    revision_mode_prompt = _build_revision_mode_prompt_context(rfq)
    available_tools = _get_available_tools(rfq, chat_mode)
    paragraph_mode_active = _history_uses_paragraph_mode(history)

    def _build_dynamic_system_prompt() -> str:
        current_rfq_state = dict(extracted_data)
        current_rfq_state.pop("potential_chat_history", None)
        current_rfq_state["phase"] = rfq.phase.value
        current_rfq_state["sub_status"] = rfq.sub_status.value
        document_type_value = _document_type_label(rfq.document_type)
        current_rfq_state["document_type"] = document_type_value
        current_rfq_state["revision_notes"] = rfq.revision_notes
        if document_type_value == RfqDocumentType.RFI.value:
            document_type_prompt = (
                "This document is an RFI. In every user-facing message, say RFI when "
                "referring to this record. If any static instruction says RFQ, treat it "
                "as the shared internal workflow name and substitute RFI in visible text. "
                "Collect the same fields and use the same workflow as an RFQ until "
                "costing is validated."
            )
        elif document_type_value == RfqDocumentType.POTENTIAL.value:
            document_type_prompt = (
                "This document is a Potential request. It must use the dedicated "
                "Potential chatbot until it is converted to RFQ."
            )
        else:
            document_type_prompt = "This document is an RFQ. Use RFQ in user-facing wording."

        if rfq.sub_status == RfqSubStatus.PENDING_FOR_VALIDATION:
            missing_fields_prompt = (
                f"The {document_type_value} has been successfully submitted and is currently "
                "PENDING_FOR_VALIDATION. The data entry phase is completely finished. "
                "DO NOT ask the user for any more fields or missing information. "
                f"Simply inform them that the {document_type_value} is waiting for validator approval."
            )
        else:
            missing_fields_prompt = _build_missing_fields_prompt(
                chat_mode,
                current_rfq_state,
                prioritize_rfq_files=paragraph_mode_active,
            )

        mode_specific_instructions = """CRITICAL INSTRUCTION:
1. Look at the CURRENT RFQ DATABASE STATE above. 
2. NEVER ask the user for information that is already populated in this JSON.
3. Use the populated fields and the missing-fields engine to determine exactly which step of the checklist you are currently on.
4. If the user replies with "1" or "2" to choose between step-by-step guidance and paragraph mode, treat that reply as a conversational command only. THIS IS NOT RFQ DATA. You MUST NOT call `updateFormFields` for it or save it into any RFQ field.
5. If the user selects Option 2 or says they want to provide a paragraph, your immediate response MUST ONLY be: 'Great! Please paste your entire RFQ paragraph below.'
6. After the user pastes the paragraph, extract every possible field across ALL relevant steps and call `updateFormFields` immediately. If the paragraph includes line-level yearly volumes or logistics per part, save them in `volumes`.
7. If you extract a product_name from the paragraph, you MUST immediately call `retrieveProducts` for that specific product before asking for manual costing details.
8. Then use the MISSING_FIELDS_PROMPT to identify only the missing fields for the CURRENT step.
9. STATE RECONCILIATION IS MANDATORY: compare the recent chat history against the CURRENT RFQ DATABASE STATE on every turn and save any missing data immediately with `updateFormFields`.
10. On every relevant turn, you MUST emit the native `updateFormFields` tool call so the frontend form stays synchronized with the latest data, but ONLY for explicit business data intended for the RFQ form.
11. Whenever the user provides a delivery destination, customer location, or country, you MUST normalize `delivery_zone` to exactly one of these 7 approved values before calling `updateFormFields`: `Europe`, `Africa`, `India`, `North America`, `South America`, `China / South Pacific`, `Korea / Japan`.
12. If you cannot confidently map a location or country to one of those 7 approved `delivery_zone` values, ask the user to clarify instead of guessing. If you need the user to choose a zone explicitly, present only those exact 7 options.
13. If a tool response or your own reasoning gives you a canonical `delivery_zone`, you MUST immediately persist that exact canonical string with `updateFormFields`.
14. If you identify missing data from the recent history, do not send a conversational acknowledgment before calling the tool.
15. If the MISSING_FIELDS_PROMPT says `total_target_to`, `to_total`, `zone_manager_email`, `validator_email`, or `validator_role` are missing, you MUST generate or retrieve them yourself. You MUST NOT ask the user to manually provide them.
16. Your follow-up text after paragraph extraction should ONLY list the specific missing fields for the CURRENT step as a clean numbered list.
17. Combine missing-fields guidance into ONE single concise message. Do not repeat the same section header or send two separate text blocks for the same turn.
18. NEVER type raw JSON, `tooluses`, or function-call payloads in your visible response. Use native tool calling only.
19. If the request document_type is POTENTIAL, do NOT ask for detailed NEW_RFQ fields until it is converted to RFQ.
20. If the RFQ sub_status is REVISION_REQUESTED, treat it as an editable RFQ revision workflow. Do NOT claim the user must return to NEW_RFQ before updates can be saved.
21. If the RFQ sub_status is REVISION_REQUESTED, you may update already-populated fields when the user wants to revise them.
22. If the RFQ sub_status is REVISION_REQUESTED, NEVER use any tool to submit or change RFQ status. When the user says the updates are finished, instruct them to click the physical "Submit Updates" button at the top of their screen."""

        if (
            paragraph_mode_active
            and chat_mode != "potential"
            and not _is_field_filled(current_rfq_state, "rfq_files")
        ):
            mode_specific_instructions += (
                "\n23. PARAGRAPH MODE FILE PRIORITY: Because the user chose paragraph mode, "
                "you MUST keep the exact Step 1 order. If `rfq_files` is still missing once "
                "all earlier required Step 1 fields are complete, your very next question "
                "MUST be the RFQ files upload question before any later remaining Step 1 field."
            )

        if pre_submission_modify_turn and chat_mode != "potential":
            mode_specific_instructions += (
                "\n24. PRE-SUBMISSION MODIFICATION MODE: The user is updating one or more "
                "already-existing fields before submission. You MUST ONLY extract and save "
                "the field(s) explicitly mentioned in the user's latest message. Do NOT "
                "resume the normal step-by-step checklist. Do NOT ask for downstream fields "
                "that come after the updated field. After saving the requested update(s), stop."
            )

        return f"""{base_system_prompt}

=== MISSING_FIELDS_PROMPT ===
{missing_fields_prompt}

=== REVISION_MODE_CONTEXT ===
{revision_mode_prompt}

=== DOCUMENT_TYPE_CONTEXT ===
{document_type_prompt}

=== CURRENT RFQ DATABASE STATE ===
Review this JSON to know exactly what has already been collected:
{json.dumps(current_rfq_state, indent=2)}

{mode_specific_instructions}
"""

    # Initialize tool calls tracking for the UI badge
    tool_calls_used = []
    final_text = ""
    auto_redirect = False

    try:
        dynamic_system_prompt = _build_dynamic_system_prompt()
    except Exception as exc:
        logger.exception(
            "Failed to build dynamic chat prompt for RFQ %s.",
            rfq.rfq_id,
        )
        final_text = (
            "**System error.**\n\n"
            "- The assistant could not prepare this chat turn.\n"
            f"- Details: `{str(exc).strip() or exc.__class__.__name__}`\n"
            "- Please try again.\n"
        )
        final_text = _append_assistant_text_if_new(history, final_text)
        if not await _persist_chat_state(
            db=db,
            rfq=rfq,
            extracted_data=extracted_data,
            history=history,
            chat_mode=chat_mode,
        ):
            return ChatResponse(response=_build_chat_persistence_error_text())
        return ChatResponse(response=final_text)

    # Prep messages for OpenAI
    messages_for_llm = [
        {"role": "system", "content": dynamic_system_prompt},
        *sliced_history
    ]
    
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
            # Guard: if the LLM wants to submit but the submit confirmation question has not
            # been shown yet, intercept submitValidation but still execute any other tool
            # calls first (e.g., updateFormFields) so form data is not lost.
            if any(tc["name"] == "submitValidation" for tc in normalized_tool_calls):
                _submit_q = _build_submit_validation_question(rfq)
                if (
                    _normalize_prompt_block_text(_submit_q)
                    not in _normalize_prompt_block_text(last_assistant_text)
                    and not _SUBMIT_QUESTION_RE.search(last_assistant_text)
                ):
                    non_submit_calls = [
                        tc for tc in normalized_tool_calls if tc["name"] != "submitValidation"
                    ]
                    if non_submit_calls:
                        _guard_asst_msg = _build_tool_call_assistant_message(non_submit_calls)
                        history.append(_guard_asst_msg)
                        async with httpx.AsyncClient(
                            timeout=httpx.Timeout(INTERNAL_TOOL_TIMEOUT_SECONDS)
                        ) as http_client:
                            _guard_tool_msgs, _ = await _execute_tool_calls(
                                tool_calls=non_submit_calls,
                                http_client=http_client,
                                db=db,
                                db3=db3,
                                rfq=rfq,
                                current_user=current_user,
                                extracted_data=extracted_data,
                                chat_mode=chat_mode,
                                tool_calls_used=tool_calls_used,
                            )
                            for _guard_tool_msg in _guard_tool_msgs:
                                history.append(_guard_tool_msg)
                    final_text = _append_assistant_text_if_new(history, _submit_q)
                    if not await _persist_chat_state(
                        db=db,
                        rfq=rfq,
                        extracted_data=extracted_data,
                        history=history,
                        chat_mode=chat_mode,
                    ):
                        return ChatResponse(response=_build_chat_persistence_error_text())
                    return ChatResponse(response=final_text)

            history.append(assistant_tool_message)
            messages_for_llm.append(assistant_tool_message)
            internal_contact_blocked_this_turn = False

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
                internal_contact_blocked_this_turn = (
                    internal_contact_blocked_this_turn
                    or _tool_messages_indicate_internal_contact_blocked(tool_messages)
                )
                for tool_message in tool_messages:
                    history.append(tool_message)
                    messages_for_llm.append(tool_message)

            rfq.rfq_data = extracted_data
            messages_for_llm[0] = {
                "role": "system",
                "content": _build_dynamic_system_prompt(),
            }

            successful_submit_payload = _extract_successful_submit_validation_payload(
                tool_messages
            )
            if successful_submit_payload is not None:
                final_text = _build_submit_validation_success_text(
                    rfq,
                    successful_submit_payload,
                )
                final_text = _append_assistant_text_if_new(history, final_text)
            else:
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
                        internal_contact_blocked_this_turn = (
                            internal_contact_blocked_this_turn
                            or _tool_messages_indicate_internal_contact_blocked(
                                follow_up_tool_messages
                            )
                        )
                        for tool_message in follow_up_tool_messages:
                            history.append(tool_message)
                            messages_for_llm.append(tool_message)

                    rfq.rfq_data = extracted_data
                    messages_for_llm[0] = {
                        "role": "system",
                        "content": _build_dynamic_system_prompt(),
                    }

                    successful_submit_payload = _extract_successful_submit_validation_payload(
                        follow_up_tool_messages
                    )
                    if successful_submit_payload is not None:
                        final_text = _build_submit_validation_success_text(
                            rfq,
                            successful_submit_payload,
                        )
                    else:
                        final_completion = await client.chat.completions.create(
                            model="gpt-5.2",
                            messages=messages_for_llm,
                            temperature=0.2,
                        )
                        final_text = (final_completion.choices[0].message.content or "").strip()
                else:
                    final_text = (follow_up_message.content or "").strip()

                final_text = _sanitize_assistant_text(final_text)
                final_text = _rewrite_submit_prompt_to_modify_prompt_if_needed(
                    text=final_text,
                    rfq=rfq,
                    chat_mode=chat_mode,
                    extracted_data=extracted_data,
                )
                if (
                    internal_contact_blocked_this_turn
                    and not _text_explains_internal_contact_rejection(final_text)
                ):
                    final_text = _build_user_facing_fallback_text(
                        rfq=rfq,
                        chat_mode=chat_mode,
                        extracted_data=extracted_data,
                        user_message=req.message,
                        force_internal_contact_explanation=True,
                    )
                if not final_text:
                    final_text = _build_user_facing_fallback_text(
                        rfq=rfq,
                        chat_mode=chat_mode,
                        extracted_data=extracted_data,
                        user_message=req.message,
                        force_internal_contact_explanation=internal_contact_blocked_this_turn,
                    )
                if (
                    pre_submission_modify_turn
                    and _tool_messages_include_clean_update_form_success(tool_messages)
                ):
                    final_text = (
                        "The requested field has been updated.\n\n"
                        + _build_modify_before_submission_question()
                    )
                final_text = _append_assistant_text_if_new(history, final_text)

        else:
            final_text = (ai_message.content or "").strip()
            final_text = _sanitize_assistant_text(final_text)
            final_text = _rewrite_submit_prompt_to_modify_prompt_if_needed(
                text=final_text,
                rfq=rfq,
                chat_mode=chat_mode,
                extracted_data=extracted_data,
            )
            if not final_text:
                final_text = _build_user_facing_fallback_text(
                    rfq=rfq,
                    chat_mode=chat_mode,
                    extracted_data=extracted_data,
                    user_message=req.message,
                )
            if pre_submission_modify_turn:
                final_text = _build_modify_fields_follow_up_text()
            # Guard: never re-ask for modifications right after "submit later" reply
            if (
                chat_mode != "potential"
                and _normalize_prompt_block_text(last_assistant_text)
                    == _normalize_prompt_block_text(_build_submit_later_text())
                and _normalize_prompt_block_text(final_text)
                    == _normalize_prompt_block_text(_build_modify_fields_follow_up_text())
            ):
                final_text = "Understood!"
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

    if not await _persist_chat_state(
        db=db,
        rfq=rfq,
        extracted_data=extracted_data,
        history=history,
        chat_mode=chat_mode,
    ):
        return ChatResponse(
            response=_build_chat_persistence_error_text(),
            tool_calls_used=tool_calls_used if tool_calls_used else None,
            auto_redirect=auto_redirect or None,
        )

    return ChatResponse(
        response=final_text,
        tool_calls_used=tool_calls_used if tool_calls_used else None,
        auto_redirect=auto_redirect or None,
    )
