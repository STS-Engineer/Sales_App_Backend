from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.user import User, UserRole
from app.schemas.auth import ApproveUserRequest, UserOut
from app.schemas.user import RoleUpdateRequest, UserOut as UserOutFull
from app.services.user_admin import apply_user_role_update, delete_user_account

router = APIRouter(prefix="/api/owner", tags=["owner"])


@router.get("/users", response_model=list[UserOutFull])
async def list_all_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(
        select(User)
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
    return await apply_user_role_update(
        user,
        body.role,
        db,
        region=body.region,
        is_approved=True,
    )


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
    return await apply_user_role_update(
        user,
        body.role,
        db,
        is_approved=body.is_approved,
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    await delete_user_account(user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
