from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.user import User, UserRole, UserRoleAssignment
from app.schemas.user import RoleUpdateRequest, UserOut
from app.services.user_admin import apply_user_role_update, delete_user_account

router = APIRouter(prefix="/api/users", tags=["users"])


async def _load_user_roles(db: AsyncSession, email: str) -> list[str]:
    """Return sorted list of all roles assigned to a user."""
    result = await db.execute(
        select(UserRoleAssignment.role).where(UserRoleAssignment.user_email == email)
    )
    return sorted(result.scalars().all())


@router.get("/pending", response_model=list[UserOut])
async def list_pending_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """Returns all users awaiting approval. Owner only."""
    result = await db.execute(
        select(User).where(User.is_approved.is_(False)).order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    if not users:
        return []

    user_emails = [u.email for u in users]
    rows = await db.execute(
        select(UserRoleAssignment.user_email, UserRoleAssignment.role).where(
            UserRoleAssignment.user_email.in_(user_emails)
        )
    )
    role_map: dict[str, list[str]] = {}
    for row in rows:
        role_map.setdefault(row[0], []).append(row[1])

    return [
        {
            "user_id": u.user_id,
            "email": u.email,
            "full_name": u.full_name,
            "role": u.role,
            "roles": sorted(role_map.get(u.email, [u.role.value])),
            "is_approved": u.is_approved,
            "region": u.region,
            "created_at": u.created_at,
        }
        for u in users
    ]


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

    updated = await apply_user_role_update(
        user,
        body.role,
        db,
        is_approved=body.is_approved,
        extra_roles=body.roles,
    )
    return {
        "user_id": updated.user_id,
        "email": updated.email,
        "full_name": updated.full_name,
        "role": updated.role,
        "roles": await _load_user_roles(db, updated.email),
        "is_approved": updated.is_approved,
        "region": updated.region,
        "created_at": updated.created_at,
    }


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
