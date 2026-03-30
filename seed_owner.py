"""
Development-only Owner bootstrap.

Creates an approved OWNER account if one does not already exist. The seeded
credentials can be overridden with environment variables:

    DEV_OWNER_EMAIL
    DEV_OWNER_PASSWORD
    DEV_OWNER_FULL_NAME
    DEV_OWNER_REGION

Run from the repository root:
    .venv\\Scripts\\python.exe Backend\\seed_owner.py
"""

import asyncio
import os

from sqlalchemy import select

import app.models  # noqa: F401  Ensures model metadata is loaded.
from app.database import async_session_maker, engine
from app.models.user import User, UserRole

DEFAULT_OWNER_EMAIL = os.getenv("DEV_OWNER_EMAIL", "dev.owner@example.com")
DEFAULT_OWNER_PASSWORD = os.getenv("DEV_OWNER_PASSWORD", "ChangeMe123!")
DEFAULT_OWNER_FULL_NAME = os.getenv("DEV_OWNER_FULL_NAME", "Development Owner")
DEFAULT_OWNER_REGION = os.getenv("DEV_OWNER_REGION", "GLOBAL")


async def seed_owner_if_missing() -> bool:
    async with async_session_maker() as session:
        existing_owner_result = await session.execute(
            select(User).where(
                User.role == UserRole.OWNER,
                User.is_approved.is_(True),
            )
        )
        existing_owner = existing_owner_result.scalar_one_or_none()
        if existing_owner:
            print(f"Approved OWNER already exists: {existing_owner.email}")
            return False

        existing_email_result = await session.execute(
            select(User).where(User.email == DEFAULT_OWNER_EMAIL)
        )
        user = existing_email_result.scalar_one_or_none()
        if user is None:
            user = User(
                email=DEFAULT_OWNER_EMAIL,
                full_name=DEFAULT_OWNER_FULL_NAME,
                role=UserRole.OWNER,
                is_approved=True,
                region=DEFAULT_OWNER_REGION,
            )
            session.add(user)
        else:
            user.full_name = DEFAULT_OWNER_FULL_NAME
            user.role = UserRole.OWNER
            user.is_approved = True
            user.region = DEFAULT_OWNER_REGION

        user.set_password(DEFAULT_OWNER_PASSWORD)
        await session.commit()

    print("Seeded development OWNER account.")
    print(f"Email: {DEFAULT_OWNER_EMAIL}")
    print(f"Password: {DEFAULT_OWNER_PASSWORD}")
    return True


async def main() -> None:
    await seed_owner_if_missing()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
