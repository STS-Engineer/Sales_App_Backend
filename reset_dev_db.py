"""
Development-only database reset.

This intentionally drops and recreates all tables from the current SQLAlchemy
models, then seeds the validation matrix. It is meant for the current overhaul
workflow where schema preservation is not required.

Run from the repository root:
    .venv\\Scripts\\python.exe Backend\\reset_dev_db.py
"""

import asyncio

import app.models  # noqa: F401  Ensures all model tables are registered on Base.metadata.
from app.database import Base, engine
from seed_owner import seed_owner_if_missing
from seed import seed_validation_matrix


async def reset_dev_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    await seed_validation_matrix()
    await seed_owner_if_missing()
    await engine.dispose()
    print("Development database reset complete.")


if __name__ == "__main__":
    asyncio.run(reset_dev_db())
