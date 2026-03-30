from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.user import User, UserRole
from app.schemas.user import RoleUpdateRequest, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/pending", response_model=list[UserOut])
async def list_pending_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """Returns all users awaiting approval. Owner only."""
    result = await db.execute(
        select(User).where(User.is_approved.is_(False)).order_by(User.created_at.desc())
    )
    return result.scalars().all()


@router.put("/{user_id}/role", response_model=UserOut)
async def update_user_role(
    user_id: str,
    body: RoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """Approve a pending user and assign their role. Owner only."""
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user.role = body.role
    user.is_approved = True
    await db.commit()
    await db.refresh(user)
    return user
