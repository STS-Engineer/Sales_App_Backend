from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import require_role
from app.models.user import User, UserRole
from app.schemas.auth import ApproveUserRequest, UserOut
from app.schemas.user import RoleUpdateRequest, UserOut as UserOutFull
from app.utils import emails

router = APIRouter(prefix="/api/owner", tags=["owner"])


def _format_role_label(role: UserRole) -> str:
    return role.value.replace("_", " ").title()


async def _apply_user_role_update(
    user: User,
    role: UserRole,
    db: AsyncSession,
    *,
    region: str | None = None,
) -> User:
    if user.role == UserRole.OWNER:
        raise HTTPException(status_code=400, detail="Owner role cannot be edited here.")

    user.role = role
    user.is_approved = True
    if region is not None:
        user.region = region

    await db.commit()
    await db.refresh(user)
    emails.send_approval_email(
        user.email,
        _format_role_label(role),
        settings.frontend_url,
    )
    return user


@router.get("/users", response_model=list[UserOutFull])
async def list_all_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(
        select(User)
        .where(User.role != UserRole.OWNER)
        .order_by(User.created_at.desc())
    )
    return result.scalars().all()


@router.post("/users/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str,
    body: ApproveUserRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if user.is_approved:
        raise HTTPException(status_code=400, detail="User is already approved.")
    return await _apply_user_role_update(user, body.role, db, region=body.region)


@router.put("/users/{user_id}/role", response_model=UserOutFull)
async def update_user_role(
    user_id: str,
    body: RoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return await _apply_user_role_update(user, body.role, db)
