from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User, UserRole
from app.utils import emails


def format_role_label(role: UserRole) -> str:
    return role.value.replace("_", " ").title()


async def apply_user_role_update(
    user: User,
    role: UserRole,
    db: AsyncSession,
    *,
    region: str | None = None,
    is_approved: bool | None = None,
) -> User:
    if user.role == UserRole.OWNER:
        raise HTTPException(status_code=400, detail="Owner accounts cannot be edited here.")
    if role == UserRole.OWNER:
        raise HTTPException(status_code=400, detail="Owner role cannot be assigned here.")

    was_approved = user.is_approved

    user.role = role
    if is_approved is not None:
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
