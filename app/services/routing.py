import unicodedata

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product_line_routing import ProductLineRouting, ProductLineRoutingRole
from app.models.routing_setting_viewers import RoutingSettingViewer
from app.models.validation_matrix import ValidationMatrix

# Fixed escalation emails
# N-3 (KAM): defaults to created_by_email (self-validation)
# N-2 (Zone Manager): canonical delivery-zone routing
N2_ZONE_EMAIL = "franck.lagadec@avocarbon.com"
N2_AMERICAS_EMAIL = "dean.hayward@avocarbon.com"
N2_ASIA_EAST_EMAIL = "tao.ren@avocarbon.com"
N2_ASIA_SOUTH_EMAIL = "ramkumar.p@avocarbon.com"
# N-1 (VP Sales)
N1_VP_EMAIL = "eric.suszylo@avocarbon.com"
# N (CEO - above N-1 threshold)
N0_CEO_EMAIL = "olivier.spicker@avocarbon.com"


def _normalize_zone_token(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    normalized = normalized.replace("_", " ").replace("-", " ").replace("/", " / ")
    return " ".join(normalized.split())


APPROVED_DELIVERY_ZONES = (
    "Europe",
    "Africa",
    "India",
    "North America",
    "South America",
    "China / South Pacific",
    "Korea / Japan",
)
ZONE_MANAGER_EMAILS = {
    "Europe": N2_ZONE_EMAIL,
    "Africa": N2_ZONE_EMAIL,
    "India": N2_ASIA_SOUTH_EMAIL,
    "North America": N2_AMERICAS_EMAIL,
    "South America": N2_AMERICAS_EMAIL,
    "China / South Pacific": N2_ASIA_EAST_EMAIL,
    "Korea / Japan": N2_ASIA_EAST_EMAIL,
}
CANONICAL_DELIVERY_ZONE_BY_TOKEN = {
    _normalize_zone_token(zone): zone for zone in APPROVED_DELIVERY_ZONES
}
DELIVERY_ZONE_ALIASES = {
    "northamerica": "North America",
    "southamerica": "South America",
    "china south pacific": "China / South Pacific",
    "korea japan": "Korea / Japan",
}


def normalize_delivery_zone(value: str | None) -> str | None:
    normalized = _normalize_zone_token(value)
    if not normalized:
        return None
    if normalized in CANONICAL_DELIVERY_ZONE_BY_TOKEN:
        return CANONICAL_DELIVERY_ZONE_BY_TOKEN[normalized]
    return DELIVERY_ZONE_ALIASES.get(normalized)


def get_zone_manager_email(delivery_zone: str | None) -> tuple[str | None, str | None]:
    canonical_zone = normalize_delivery_zone(delivery_zone)
    if not canonical_zone:
        return None, None
    return ZONE_MANAGER_EMAILS.get(canonical_zone), canonical_zone


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().casefold()


async def _get_validation_matrix_by_identifier(
    db: AsyncSession,
    identifier: str | None,
) -> ValidationMatrix | None:
    normalized_identifier = str(identifier or "").strip()
    if not normalized_identifier:
        return None

    result = await db.execute(
        select(ValidationMatrix).where(
            or_(
                func.lower(ValidationMatrix.product_line) == normalized_identifier.casefold(),
                func.lower(ValidationMatrix.acronym) == normalized_identifier.casefold(),
            )
        )
    )
    return result.scalar_one_or_none()


async def resolve_product_line_context(
    db: AsyncSession,
    *,
    identifier: str | None = None,
    product_line: str | None = None,
    acronym: str | None = None,
) -> dict[str, str] | None:
    matrix = None

    for candidate in (acronym, product_line, identifier):
        matrix = await _get_validation_matrix_by_identifier(db, candidate)
        if matrix is not None:
            break

    if matrix is None:
        return None

    return {
        "product_line": str(matrix.product_line or "").strip(),
        "acronym": str(matrix.acronym or "").strip(),
    }


async def resolve_product_line_role_emails(
    db: AsyncSession,
    *,
    role: ProductLineRoutingRole,
    identifier: str | None = None,
    product_line: str | None = None,
    acronym: str | None = None,
) -> list[str]:
    context = await resolve_product_line_context(
        db,
        identifier=identifier,
        product_line=product_line,
        acronym=acronym,
    )
    if context is None:
        return []

    result = await db.execute(
        select(ProductLineRouting.email)
        .where(
            ProductLineRouting.product_line == context["product_line"],
            ProductLineRouting.role == role,
        )
        .order_by(ProductLineRouting.id.asc())
    )
    return [str(e or "").strip() for e in result.scalars().all() if e]


async def resolve_product_line_role_email(
    db: AsyncSession,
    *,
    role: ProductLineRoutingRole,
    identifier: str | None = None,
    product_line: str | None = None,
    acronym: str | None = None,
) -> str | None:
    emails_list = await resolve_product_line_role_emails(
        db,
        role=role,
        identifier=identifier,
        product_line=product_line,
        acronym=acronym,
    )
    return emails_list[0] if emails_list else None


async def resolve_product_line_role_assignments_multi(
    db: AsyncSession,
    *,
    role: ProductLineRoutingRole,
    identifier: str | None = None,
    product_line: str | None = None,
    acronym: str | None = None,
) -> list[dict[str, str]]:
    context = await resolve_product_line_context(
        db,
        identifier=identifier,
        product_line=product_line,
        acronym=acronym,
    )
    if context is None:
        return []

    emails_list = await resolve_product_line_role_emails(
        db,
        role=role,
        product_line=context["product_line"],
    )
    return [{**context, "email": email} for email in emails_list]


async def resolve_product_line_role_assignment(
    db: AsyncSession,
    *,
    role: ProductLineRoutingRole,
    identifier: str | None = None,
    product_line: str | None = None,
    acronym: str | None = None,
) -> dict[str, str] | None:
    entries = await resolve_product_line_role_assignments_multi(
        db,
        role=role,
        identifier=identifier,
        product_line=product_line,
        acronym=acronym,
    )
    return entries[0] if entries else None


async def get_assigned_product_line_acronyms(
    db: AsyncSession,
    *,
    role: ProductLineRoutingRole,
    email: str | None,
) -> list[str]:
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return []

    result = await db.execute(
        select(ValidationMatrix.acronym)
        .join(
            ProductLineRouting,
            ProductLineRouting.product_line == ValidationMatrix.product_line,
        )
        .where(
            ProductLineRouting.role == role,
            func.lower(ProductLineRouting.email) == normalized_email,
        )
        .order_by(ValidationMatrix.acronym.asc())
    )
    return [str(value or "").strip().upper() for value in result.scalars().all() if value]


async def get_viewer_product_line_acronyms(
    db: AsyncSession,
    *,
    email: str | None,
) -> list[str]:
    """Return product line acronyms for which the given email is a Viewer."""
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return []

    result = await db.execute(
        select(ValidationMatrix.acronym)
        .join(
            RoutingSettingViewer,
            RoutingSettingViewer.product_line == ValidationMatrix.product_line,
        )
        .where(func.lower(RoutingSettingViewer.user_email) == normalized_email)
        .order_by(ValidationMatrix.acronym.asc())
    )
    return [str(v or "").strip().upper() for v in result.scalars().all() if v]


async def user_is_routing_viewer_for_rfq(
    db: AsyncSession,
    user_email: str | None,
    rfq,
) -> bool:
    """Return True if user_email is a Viewer for the RFQ's product line."""
    normalized_email = _normalize_email(user_email)
    if not normalized_email or not rfq.product_line_acronym:
        return False

    context = await resolve_product_line_context(db, acronym=rfq.product_line_acronym)
    if context is None:
        return False

    result = await db.execute(
        select(RoutingSettingViewer)
        .where(
            RoutingSettingViewer.product_line == context["product_line"],
            func.lower(RoutingSettingViewer.user_email) == normalized_email,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def calculate_pte(target_price: float, qty_per_year: int) -> float:
    """
    Calculates the Potential Total Exposure (PTE) in KEUR.
    PTE = (target_price * qty_per_year) / 1000
    """
    return (target_price * qty_per_year) / 1000


async def assign_validator(
    product_line: str,
    pte: float,
    commercial_email: str,
    db: AsyncSession,
    delivery_zone: str | None = None,
) -> str:
    """
    Assigns a validator email based on the PTE and the product line thresholds.
    """
    result = await db.execute(
        select(ValidationMatrix).where(ValidationMatrix.product_line == product_line)
    )
    matrix = result.scalar_one_or_none()
    if matrix is None:
        raise ValueError(f"Unknown product line: '{product_line}'")

    if pte <= matrix.n3_kam_limit:
        return commercial_email
    if pte <= matrix.n2_zone_limit:
        if delivery_zone:
            zone_manager_email, _ = get_zone_manager_email(delivery_zone)
            if not zone_manager_email:
                raise ValueError(f"Unknown delivery zone: '{delivery_zone}'")
            return zone_manager_email
        return N2_ZONE_EMAIL
    if pte <= matrix.n1_vp_limit:
        return N1_VP_EMAIL
    return N0_CEO_EMAIL