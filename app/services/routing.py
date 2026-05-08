import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.validation_matrix import ValidationMatrix

# Fixed escalation emails
# N-3 (KAM): defaults to created_by_email (self-validation)
# N-2 (Zone Manager): canonical delivery-zone routing
N2_ZONE_EMAIL = "franck.lagadec@avocarbon.com"
N2_AMERICAS_EMAIL = "dean.hayward@avocarbon.com"
N2_ASIA_EAST_EMAIL = "tao.ren@avocarbon.com"
N2_ASIA_SOUTH_EMAIL = "eipe.thomas@avocarbon.com"
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
