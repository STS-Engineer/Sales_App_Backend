from datetime import datetime
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.schemas.offer_preparation import OfferPreparationOut
from app.models.rfq import RfqDocumentType, RfqPhase, RfqSubStatus
from app.schemas.potential import PotentialOut


class RfqOut(BaseModel):
    rfq_id: str
    document_type: RfqDocumentType = RfqDocumentType.RFQ
    phase: RfqPhase
    sub_status: RfqSubStatus
    product_line_acronym: str | None
    contact_id: int | None
    zone_manager_email: str | None
    created_by_email: str
    rfq_data: dict[str, Any] | None
    chat_history: list[dict[str, Any]] | None
    costing_files: list[dict[str, Any]] | None
    costing_file_state: dict[str, Any] | None
    potential: PotentialOut | None = None
    offer_preparation: OfferPreparationOut | None = None
    rejection_reason: str | None
    revision_notes: str | None
    autopsy_notes: str | None
    approved_at: datetime | None
    rejected_at: datetime | None
    last_notification_sent_at: datetime | None = None
    follow_up_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AuditLogOut(BaseModel):
    log_id: str
    rfq_id: str
    action: str
    performed_by: str
    timestamp: datetime

    model_config = {"from_attributes": True}


class NotificationLogOut(BaseModel):
    log_id: str
    rfq_id: str
    recipient_email: str
    email_type: str
    sent_at: datetime

    model_config = {"from_attributes": True}


class RfqFxRateOut(BaseModel):
    currency_code: str
    eur_rate: float
    fallback_used: bool


class ProductItem(BaseModel):
    part_number: str | None = None
    revision_level: str | None = None
    quantity: float | None = None
    target_price: float | None = None
    currency: str | None = None
    target_price_is_estimated: bool | None = None
    target_to: float | None = None

    model_config = ConfigDict(extra="allow")

    @field_validator("quantity", "target_price", "target_to", mode="before")
    @classmethod
    def blank_numeric_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def compute_target_to(self) -> "ProductItem":
        if self.quantity is not None and self.target_price is not None:
            self.target_to = self.quantity * self.target_price
        return self


class RfqDataPayload(BaseModel):
    products: list[ProductItem] | None = None
    total_target_to: float | None = None
    po_date: str | None = None
    ppap_date: str | None = None

    model_config = ConfigDict(extra="allow")


def _pick_first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_currency_code(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return re.sub(r"[^A-Za-z]", "", cleaned).upper() or None


def _coerce_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None

    text = str(value).strip().replace("\u00a0", " ")
    if not text:
        return None
    text = re.sub(r"[^0-9,.\-]", "", text.replace(" ", ""))
    if not text or text in {"-", ".", ","}:
        return None

    last_comma = text.rfind(",")
    last_dot = text.rfind(".")
    if last_comma != -1 and last_dot != -1:
        if last_comma > last_dot:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif last_comma != -1:
        comma_count = text.count(",")
        if comma_count == 1 and re.search(r",\d{1,2}$", text):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number else None


def _coerce_bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None

    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"true", "1", "yes", "y", "estimated", "estimated by avocarbon"}:
        return True
    if text in {"false", "0", "no", "n", "official", "official customer price", "given by customer"}:
        return False
    return None


def _normalize_product_item(raw_item: Any) -> dict[str, Any] | None:
    if isinstance(raw_item, ProductItem):
        item = raw_item.model_dump()
    elif isinstance(raw_item, dict):
        item = dict(raw_item)
    else:
        return None

    part_number = _clean_text(
        _pick_first(
            item,
            (
                "part_number",
                "partNumber",
                "customer_pn",
                "customerPn",
                "pn",
                "part_no",
                "partNo",
            ),
        )
    )
    revision_level = _clean_text(
        _pick_first(
            item,
            (
                "revision_level",
                "revisionLevel",
                "revision",
                "rev",
            ),
        )
    )
    quantity = _coerce_float_or_none(
        _pick_first(
            item,
            (
                "quantity",
                "qty",
                "annual_volume",
                "annualVolume",
                "qty_per_year",
                "qtyPerYear",
            ),
        )
    )
    target_price = _coerce_float_or_none(
        _pick_first(
            item,
            (
                "target_price",
                "targetPrice",
                "price",
            ),
        )
    )
    currency = _normalize_currency_code(
        _pick_first(
            item,
            (
                "currency",
                "target_price_currency",
                "targetPriceCurrency",
                "target_currency",
                "targetCurrency",
            ),
        )
    )
    target_price_is_estimated = _coerce_bool_or_none(
        _pick_first(
            item,
            (
                "target_price_is_estimated",
                "targetPriceIsEstimated",
                "price_source",
                "priceSource",
            ),
        )
    )
    target_to = (
        quantity * target_price
        if quantity is not None and target_price is not None
        else _coerce_float_or_none(
            _pick_first(item, ("target_to", "targetTo", "turnover"))
        )
    )

    normalized = {
        "part_number": part_number,
        "revision_level": revision_level,
        "quantity": quantity,
        "target_price": target_price,
        "currency": currency,
        "target_price_is_estimated": target_price_is_estimated,
        "target_to": target_to,
    }
    if not any(value not in (None, "") for value in normalized.values()):
        return None
    return normalized


def _normalize_products_input(raw_products: Any) -> list[dict[str, Any]]:
    products_value = raw_products
    if isinstance(products_value, str):
        try:
            products_value = json.loads(products_value)
        except json.JSONDecodeError:
            products_value = None

    if isinstance(products_value, dict):
        products_value = [products_value]
    if not isinstance(products_value, list):
        return []

    normalized_products: list[dict[str, Any]] = []
    for item in products_value:
        normalized_item = _normalize_product_item(item)
        if normalized_item is not None:
            normalized_products.append(normalized_item)
    return normalized_products


def _legacy_product_from_data(data: dict[str, Any]) -> dict[str, Any] | None:
    legacy_item = {
        "part_number": data.get("customer_pn") or data.get("customerPn"),
        "revision_level": data.get("revision_level") or data.get("revisionLevel"),
        "quantity": data.get("annual_volume") or data.get("qty_per_year") or data.get("qtyPerYear"),
        "target_price": _pick_first(
            data,
            (
                "target_price_local",
                "targetPriceLocal",
                "target_price_eur",
                "targetPriceEur",
                "targetPrice",
            ),
        ),
        "currency": data.get("target_price_currency") or data.get("targetPriceCurrency"),
        "target_price_is_estimated": _pick_first(
            data,
            ("target_price_is_estimated", "targetPriceIsEstimated"),
        ),
    }
    return _normalize_product_item(legacy_item)


def normalize_rfq_data_products(
    data: dict[str, Any] | None,
    *,
    products_authoritative: bool = False,
) -> dict[str, Any]:
    """Return rfq_data with canonical products and legacy first-row mirrors."""
    normalized = dict(data or {})
    products = _normalize_products_input(normalized.get("products"))
    legacy_price_source = _coerce_bool_or_none(
        _pick_first(
            normalized,
            ("target_price_is_estimated", "targetPriceIsEstimated"),
        )
    )
    if not products and not products_authoritative:
        legacy_product = _legacy_product_from_data(normalized)
        if legacy_product is not None:
            products = [legacy_product]

    if products or products_authoritative:
        fallback_currency = _normalize_currency_code(
            normalized.get("target_price_currency") or normalized.get("targetPriceCurrency")
        )
        allow_legacy_currency_hydration = not products_authoritative
        for product in products:
            if not isinstance(product, dict):
                continue
            if product.get("currency") is None and product.get("target_price") is not None:
                if fallback_currency:
                    product["currency"] = fallback_currency
                elif allow_legacy_currency_hydration:
                    product["currency"] = "EUR"
            if (
                product.get("target_price_is_estimated") is None
                and legacy_price_source is not None
            ):
                product["target_price_is_estimated"] = legacy_price_source

        total_target_to = sum(
            product["target_to"]
            for product in products
            if isinstance(product.get("target_to"), (int, float))
        )
        normalized["products"] = products
        normalized["total_target_to"] = total_target_to
        # Only set to_total as a naive local-currency fallback when there is
        # no pre-existing FX-converted value.  The authoritative EUR value is
        # computed by _sync_rfq_product_derived_fields which has DB access for
        # live exchange-rate lookups.  Blindly overwriting it here was the root
        # cause of massive kEUR numbers in the Step-4 display.
        existing_to_total = _coerce_float_or_none(normalized.get("to_total"))
        if existing_to_total is None:
            normalized["to_total"] = total_target_to / 1000.0

        first_product = products[0] if products else {}
        if first_product:
            normalized["customer_pn"] = first_product.get("part_number") or ""
            normalized["revision_level"] = first_product.get("revision_level") or ""
            normalized["annual_volume"] = first_product.get("quantity") or ""
            normalized["target_price_local"] = (
                first_product.get("target_price")
                if first_product.get("target_price") is not None
                else ""
            )
            normalized["target_price_currency"] = (
                first_product.get("currency")
                or fallback_currency
                or ""
            )

            existing_to_total_local = _coerce_float_or_none(normalized.get("to_total_local"))
            if existing_to_total_local is None:
                shared_currency = _normalize_currency_code(normalized.get("target_price_currency"))
                if shared_currency and shared_currency != "EUR":
                    normalized["to_total_local"] = total_target_to / 1000.0

    normalized.pop("target_price_is_estimated", None)
    normalized.pop("targetPriceIsEstimated", None)
    return normalized


def get_incomplete_product_fields(
    data: dict[str, Any] | None,
    *,
    include_optional: bool = False,
) -> list[str]:
    raw_products = data.get("products") if isinstance(data, dict) else None
    products_authoritative = bool(
        isinstance(raw_products, list)
        and any(
            isinstance(product, dict) and "currency" in product
            for product in raw_products
        )
    )
    normalized = normalize_rfq_data_products(
        data,
        products_authoritative=products_authoritative,
    )
    products = normalized.get("products")
    if not isinstance(products, list) or not products:
        return ["products"]

    missing_fields: list[str] = []
    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            missing_fields.append(f"products[{index}]")
            continue
        if not _clean_text(product.get("part_number")):
            missing_fields.append(f"products[{index}].part_number")
        # Revision level is optional for RFQ products. If the user omits it while
        # providing an otherwise complete product row, we leave it blank and keep
        # the workflow moving instead of forcing a dedicated follow-up question.
        quantity = _coerce_float_or_none(product.get("quantity"))
        if quantity is None or quantity <= 0:
            missing_fields.append(f"products[{index}].quantity")
        target_price = _coerce_float_or_none(product.get("target_price"))
        if target_price is None or target_price <= 0:
            missing_fields.append(f"products[{index}].target_price")
        if not _normalize_currency_code(product.get("currency")):
            missing_fields.append(f"products[{index}].currency")
        if _coerce_bool_or_none(product.get("target_price_is_estimated")) is None:
            missing_fields.append(f"products[{index}].target_price_is_estimated")
    return missing_fields


def get_conflicting_product_currencies(data: dict[str, Any] | None) -> list[str]:
    normalized = normalize_rfq_data_products(data)
    products = normalized.get("products")
    if not isinstance(products, list):
        return []

    currencies = sorted(
        {
            currency
            for currency in (
                _normalize_currency_code(product.get("currency"))
                for product in products
                if isinstance(product, dict)
            )
            if currency
        }
    )
    return currencies if len(currencies) > 1 else []


def rfq_data_payload_to_dict(
    payload: "RfqDataPayload | dict[str, Any] | None",
) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return normalize_rfq_data_products(dict(payload), products_authoritative="products" in payload)
    return normalize_rfq_data_products(
        payload.model_dump(exclude_unset=True),
        products_authoritative=payload.products is not None,
    )



class RfqCreateRequest(BaseModel):
    """Optional body when creating a new RFQ.
    chat_mode='potential' creates a POTENTIAL document at RFQ/NEW_RFQ.
    """
    chat_mode: str = "rfq"
    document_type: RfqDocumentType = RfqDocumentType.RFQ
    rfq_data: RfqDataPayload | None = None


class ProceedToFormalRequest(BaseModel):
    """Body for POST /api/rfq/{id}/proceed-to-rfq — choose RFQ or RFI."""
    document_type: RfqDocumentType = RfqDocumentType.RFQ


class RfqDataUpdateRequest(BaseModel):
    rfq_data: RfqDataPayload


class PhaseStatusUpdateRequest(BaseModel):
    """Direct phase + sub_status update (admin/owner use)."""
    phase: RfqPhase
    sub_status: RfqSubStatus


class AutopsyRequest(BaseModel):
    """Required when an RFQ is in LOST or CANCELED sub_status."""
    rejection_reason: str
    autopsy_notes: str


class ValidateRfqRequest(BaseModel):
    """Body for POST /api/rfq/{id}/validate - Validator approve/reject."""
    approved: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_rejected(self) -> "ValidateRfqRequest":
        if not self.approved and not self.rejection_reason:
            raise ValueError("rejection_reason is required when approved=False")
        return self


class RequestRevisionRequest(BaseModel):
    comment: str


class CostingReviewRequest(BaseModel):
    """Body for POST /api/rfq/{id}/costing_review — Costing scope step."""
    scope: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_out_of_scope(self) -> "CostingReviewRequest":
        if not self.scope and not self.rejection_reason:
            raise ValueError("rejection_reason is required when scope=False")
        return self


class CostingValidationRequest(BaseModel):
    """Body for POST /api/rfq/{id}/costing_validation - Pricing approval step."""
    is_approved: bool
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def rejection_required_if_rejected(self) -> "CostingValidationRequest":
        if not self.is_approved and not self.rejection_reason:
            raise ValueError("rejection_reason is required when is_approved=False")
        return self


class AdvanceStatusRequest(BaseModel):
    """Advance an RFQ through the state machine."""
    target_phase: RfqPhase
    target_sub_status: RfqSubStatus
    notes: str | None = None
    # Required when transitioning to LOST or CANCELED
    autopsy_notes: str | None = None

    @model_validator(mode="after")
    def autopsy_required_for_terminal(self) -> "AdvanceStatusRequest":
        terminal = {RfqSubStatus.LOST, RfqSubStatus.CANCELED}
        if self.target_sub_status in terminal and not self.autopsy_notes:
            raise ValueError(
                "autopsy_notes is required when transitioning to LOST or CANCELED"
            )
        return self
