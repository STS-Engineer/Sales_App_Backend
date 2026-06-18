from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User, UserRole
from app.utils import emails


def format_role_label(role: UserRole) -> str:
    return role.value.replace("_", " ").title()


async def count_owner_accounts(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.OWNER)
    )
    return int(result.scalar_one() or 0)


async def ensure_owner_account_can_change(
    user: User,
    next_role: UserRole,
    db: AsyncSession,
) -> None:
    if user.role == UserRole.OWNER and next_role != UserRole.OWNER:
        owner_count = await count_owner_accounts(db)
        if owner_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="At least one owner account must remain active.",
            )


async def apply_user_role_update(
    user: User,
    role: UserRole,
    db: AsyncSession,
    *,
    region: str | None = None,
    is_approved: bool | None = None,
) -> User:
    await ensure_owner_account_can_change(user, role, db)

    was_approved = user.is_approved

    user.role = role
    if role == UserRole.OWNER:
        user.is_approved = True
    elif is_approved is not None:
        user.is_approved = is_approved
    if region is not None:
        user.region = region

    await db.commit()
    await db.refresh(user)

    if not was_approved and user.is_approved:
        emails.send_approval_email(
            user.email,
            format_role_label(role),
            settings.frontend_url,
        )

    return user


async def delete_user_account(user: User, db: AsyncSession) -> None:
    if user.role == UserRole.OWNER:
        owner_count = await count_owner_accounts(db)
        if owner_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="At least one owner account must remain active.",
            )

    await db.delete(user)
    await db.commit()
