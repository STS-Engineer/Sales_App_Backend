"""
seed.py - populates the validation_matrix table with confirmed KEUR thresholds.
Run with: python seed.py  (from the backend/ directory with .env set)
"""
import asyncio

from app.database import async_session_maker
from app.models.validation_matrix import ValidationMatrix

VALIDATION_MATRIX_DATA = [
    # Brushes (BRU): N-3=250 | N-2=750 | N-1=1500 | N=>1500
    {"product_line": "Brushes", "acronym": "BRU", "n3_kam_limit": 250, "n2_zone_limit": 750, "n1_vp_limit": 1500},
    # Advanced Material (ADM): N-3=200 | N-2=600 | N-1=1200 | N=>1200
    {"product_line": "Advanced Material", "acronym": "ADM", "n3_kam_limit": 200, "n2_zone_limit": 600, "n1_vp_limit": 1200},
    # Chokes (CHO): N-3=285 | N-2=857 | N-1=1714 | N=>1714
    {"product_line": "Chokes", "acronym": "CHO", "n3_kam_limit": 285, "n2_zone_limit": 857, "n1_vp_limit": 1714},
    # Friction (FRI): N-3=167 | N-2=500 | N-1=1000 | N=>1000
    {"product_line": "Friction", "acronym": "FRI", "n3_kam_limit": 167, "n2_zone_limit": 500, "n1_vp_limit": 1000},
    # Seals (SEA): N-3=333 | N-2=1000 | N-1=2000 | N=>2000
    {"product_line": "Seals", "acronym": "SEA", "n3_kam_limit": 333, "n2_zone_limit": 1000, "n1_vp_limit": 2000},
    # Assembly (ASS): N-3=400 | N-2=1200 | N-1=2400 | N=>2400
    {"product_line": "Assembly", "acronym": "ASS", "n3_kam_limit": 400, "n2_zone_limit": 1200, "n1_vp_limit": 2400},
]


async def seed_validation_matrix() -> None:
    async with async_session_maker() as session:
        for data in VALIDATION_MATRIX_DATA:
            existing = await session.get(ValidationMatrix, data["product_line"])
            if not existing:
                session.add(ValidationMatrix(**data))
        await session.commit()
    print("Validation matrix seeded successfully.")
    for row in VALIDATION_MATRIX_DATA:
        print(
            f"   {row['product_line']:<20} ({row['acronym']}) "
            f"N-3<={row['n3_kam_limit']}  N-2<={row['n2_zone_limit']}  "
            f"N-1<={row['n1_vp_limit']}  N>N-1"
        )


if __name__ == "__main__":
    asyncio.run(seed_validation_matrix())
