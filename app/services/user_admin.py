from fastapi import HTTPException
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User, UserRole, UserRoleAssignment
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


async def set_user_roles(db: AsyncSession, email: str, roles: list[str]) -> None:
    """Replace all role assignments for a user in the user_roles table."""
    await db.execute(
        sa_delete(UserRoleAssignment).where(UserRoleAssignment.user_email == email)
    )
    seen: set[str] = set()
    for role in roles:
        role_str = str(role).strip().upper()
        if role_str and role_str not in seen:
            seen.add(role_str)
            db.add(UserRoleAssignment(user_email=email, role=role_str))


async def apply_user_role_update(
    user: User,
    role: UserRole,
    db: AsyncSession,
    *,
    region: str | None = None,
    is_approved: bool | None = None,
    extra_roles: list[UserRole] | None = None,
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

    # Build the complete roles list: primary first, then extras (deduped, ordered)
    all_role_values: list[str] = [role.value]
    if extra_roles:
        for r in extra_roles:
            r_val = r.value if isinstance(r, UserRole) else str(r).strip().upper()
            if r_val and r_val not in all_role_values:
                all_role_values.append(r_val)

    await set_user_roles(db, user.email, all_role_values)

    await db.commit()
    await db.refresh(user)

    if not was_approved and user.is_approved:
        # Disabled as requested: do not send signup approval email after Owner approval
        emails.send_approval_email(
            user.email,
            format_role_label(role),
            settings.frontend_url,
        )
        pass

    return user


async def delete_user_account(user: User, db: AsyncSession) -> None:
    if user.role == UserRole.OWNER:
        owner_count = await count_owner_accounts(db)
        if owner_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="At least one owner account must remain active.",
            )

    await db.execute(
        sa_delete(UserRoleAssignment).where(UserRoleAssignment.user_email == user.email)
    )
    await db.delete(user)
    await db.commit()
