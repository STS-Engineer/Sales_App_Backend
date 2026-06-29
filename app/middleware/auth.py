from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt

from app.database import get_db
from app.models.user import User, UserRole, UserRoleAssignment
from app.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        email: str | None = payload.get("sub")
        token_type = payload.get("token_type")
        if email is None or token_type != "access":
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception

    # Load all roles from user_roles table (populated at startup for existing users)
    roles_result = await db.execute(
        select(UserRoleAssignment.role).where(UserRoleAssignment.user_email == user.email)
    )
    stored_roles = set(roles_result.scalars().all())
    # Always include primary role for backward compatibility with unregistered entries
    user.__dict__["_all_roles"] = stored_roles | {user.role.value}
    return user


def require_role(*roles: UserRole):
    """
    Returns a FastAPI dependency that enforces role-based access.
    Checks the user's primary role and any additional roles from user_roles table.
    """
    async def _checker(current_user: User = Depends(get_current_user)) -> User:
        required_values = {r.value for r in roles}
        all_user_roles: set[str] = current_user.__dict__.get(
            "_all_roles", {current_user.role.value}
        )
        if all_user_roles & required_values:
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required roles: {[r.value for r in roles]}",
        )

    return _checker
