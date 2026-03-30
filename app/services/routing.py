from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.validation_matrix import ValidationMatrix

# ── Fixed escalation emails ──────────────────────────────────────────
# N-3 (KAM): defaults to created_by_email (self-validation)
# N-2 (Zone Manager): Franck Lagadec (Europe) — default for MVP
N2_ZONE_EMAIL = "franck.lagadec@avocarbon.com"
# Alternate Zone Manager (Americas):
N2_AMERICAS_EMAIL = "dean.hayward@avocarbon.com"
# N-1 (VP Sales)
N1_VP_EMAIL = "eric.suszylo@avocarbon.com"
# N (CEO — above N-1 threshold)
N0_CEO_EMAIL = "olivier.spicker@avocarbon.com"


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
) -> str:
    """
    Assigns a validator email based on the PTE and the product line thresholds.

    Escalation ladder (KEUR):
    ┌──────────────────────┬───────┬───────┬────────┬────────┐
    │ Product Line         │ N-3   │ N-2   │ N-1    │ N (CEO)│
    ├──────────────────────┼───────┼───────┼────────┼────────┤
    │ Brushes (BRU)        │ ≤250  │ ≤750  │ ≤1500  │ >1500  │
    │ Advanced Material    │ ≤200  │ ≤600  │ ≤1200  │ >1200  │
    │ Chokes (CHO)         │ ≤285  │ ≤857  │ ≤1714  │ >1714  │
    │ Friction (FRI)       │ ≤167  │ ≤500  │ ≤1000  │ >1000  │
    │ Seals (SEA)          │ ≤333  │ ≤1000 │ ≤2000  │ >2000  │
    │ Assembly (ASS)       │ ≤400  │ ≤1200 │ ≤2400  │ >2400  │
    └──────────────────────┴───────┴───────┴────────┴────────┘
    """
    result = await db.execute(
        select(ValidationMatrix).where(ValidationMatrix.product_line == product_line)
    )
    matrix = result.scalar_one_or_none()
    if matrix is None:
        raise ValueError(f"Unknown product line: '{product_line}'")

    if pte <= matrix.n3_kam_limit:
        return commercial_email  # N-3: self-validation
    elif pte <= matrix.n2_zone_limit:
        return N2_ZONE_EMAIL     # N-2: Zone Manager (Europe default)
    elif pte <= matrix.n1_vp_limit:
        return N1_VP_EMAIL       # N-1: VP Sales
    else:
        return N0_CEO_EMAIL      # N: CEO
