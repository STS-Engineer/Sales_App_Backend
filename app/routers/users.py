from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.user import User, UserRole
from app.schemas.user import RoleUpdateRequest, UserOut
from app.services.user_admin import apply_user_role_update, delete_user_account

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
    """Update a user's role and approval state. Owner only."""
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return await apply_user_role_update(
        user,
        body.role,
        db,
        is_approved=body.is_approved,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """Delete a user account. Owner only."""
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    await delete_user_account(user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
